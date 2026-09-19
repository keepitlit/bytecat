"""LM Studio client. Local only, by construction.

The hard requirement behind this whole project is that personal file content
never leaves the machine. So this module refuses to talk to anything that is not
a loopback address, and there is no configuration option that relaxes that. If
LM Studio is down the pipeline stops -- it must never quietly fall back to a
hosted endpoint.

Stdlib urllib rather than `requests`/`openai`: fewer dependencies, and no client
library that might helpfully retry against a different base URL.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

DEFAULT_BASE = "http://127.0.0.1:1234/v1"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

EMBED_MODEL = os.environ.get("BYTECAT_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
# The chat model used by tier 4 (`enrich`) and `ask --smart`. There is no
# default: set BYTECAT_CHAT_MODEL to whatever your server has loaded.
# `ensure_up()` reports the mismatch if it is stale.
CHAT_MODEL = os.environ.get("BYTECAT_CHAT_MODEL", "")
# Set BYTECAT_LLM_AUTOSTART=1 to let `ensure_up()` run `lms server start`
# (LM Studio only). Off by default: a public tool should not launch apps.
AUTOSTART = os.environ.get("BYTECAT_LLM_AUTOSTART", "") == "1"


class LocalOnlyError(RuntimeError):
    """Raised when something tries to point the pipeline off-box."""


class LMStudioDown(RuntimeError):
    pass


def _assert_local(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise LocalOnlyError(
            f"refusing non-loopback endpoint {host!r}. bytecat only talks to a local "
            "model; this is a hard invariant, not a default."
        )


class _LocalOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Re-apply the guard to every redirect hop.

    urllib follows 301/302/303 by itself without consulting us, so checking only
    the base URL leaves a gap: a process holding the model port could bounce the
    request off-box. Python drops the request body on those codes (and refuses
    307/308 outright for a POST), so file content cannot ride along -- but the
    request itself would still leave, which is enough to beacon. Refuse instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_local(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Every request in this module goes through here, never through the module-level
# urlopen, which would use the default redirect handler.
_OPENER = urllib.request.build_opener(_LocalOnlyRedirects)


def reply_text(message: dict) -> str:
    """Pull the assistant's answer out of a chat message.

    Reasoning models split their output, and not every chat template closes the
    thinking block cleanly -- bonsai-27b under LM Studio returns the whole
    schema-constrained answer in `reasoning_content` with `content` empty. Read
    the normal field first and fall back, rather than silently labelling every
    file as unreadable. Callers already tolerate prose around the JSON.
    """
    return (message.get("content") or "").strip() or \
           (message.get("reasoning_content") or "").strip()


class LMStudio:
    def __init__(self, base: str = "", timeout: float = 120.0):
        self.base = (base or os.environ.get("BYTECAT_LLM_BASE") or DEFAULT_BASE).rstrip("/")
        _assert_local(self.base)
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------

    def _post(self, path: str, payload: dict, *, retries: int = 3) -> dict:
        _assert_local(self.base)
        url = f"{self.base}{path}"
        body = json.dumps(payload).encode()
        last: Optional[Exception] = None

        for attempt in range(retries):
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with _OPENER.open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            # HTTPError MUST be caught before URLError -- it is a subclass, and
            # catching the parent first makes every 4xx look like a dead server.
            except urllib.error.HTTPError as e:
                detail = e.read()[:300]
                raise RuntimeError(f"LM Studio {e.code} on {path}: {detail!r}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                # A model being JIT-loaded can take a while on the first call.
                time.sleep(2 * (attempt + 1))

        raise LMStudioDown(f"{url} unreachable after {retries} tries: {last}")

    # -- lifecycle --------------------------------------------------------

    def models(self) -> list[str]:
        _assert_local(self.base)
        try:
            with _OPENER.open(f"{self.base}/models", timeout=10) as r:
                return [m["id"] for m in json.loads(r.read()).get("data", [])]
        except LocalOnlyError:
            # The probe path swallows errors so a down server reads as "no
            # models". A guard violation is not that, and must not be hidden.
            raise
        except Exception:
            return []

    def ensure_up(self, *, autostart: Optional[bool] = None) -> list[str]:
        """Verify the server, optionally starting LM Studio's if needed.

        LM Studio ships with the server off, so a fresh boot would otherwise
        fail the first pipeline stage with a connection error. Autostart is
        opt-in (BYTECAT_LLM_AUTOSTART=1); any other OpenAI-compatible server
        (Ollama, llama.cpp, vLLM) is simply expected to be running.
        """
        models = self.models()
        if models:
            return models
        if autostart is None:
            autostart = AUTOSTART
        if not autostart:
            raise LMStudioDown(
                f"no model server at {self.base}. Start one (LM Studio, Ollama, "
                f"llama-server ...) or point BYTECAT_LLM_BASE at it.")

        lms = os.path.expanduser("~/.lmstudio/bin/lms")
        if os.path.exists(lms):
            subprocess.run([lms, "server", "start"], capture_output=True, timeout=60)
            for _ in range(10):
                time.sleep(2)
                models = self.models()
                if models:
                    return models
        raise LMStudioDown(
            f"could not reach or start LM Studio at {self.base}. "
            f"Try: {lms} server start"
        )

    # -- inference --------------------------------------------------------

    def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        if not texts:
            return []
        out = self._post("/embeddings", {
            "model": model or EMBED_MODEL,
            "input": texts,
        })
        # Order is not guaranteed to be echoed back; sort by index defensively.
        data = sorted(out["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    def chat(self, system: str, user: str, *, model: str = "",
             max_tokens: int = 300, temperature: float = 0.1,
             schema: Optional[dict] = None) -> str:
        """Chat completion, optionally constrained to a JSON schema.

        LM Studio implements structured output as `json_schema` and rejects
        OpenAI's older `json_object` form outright. Constraining beats hinting
        anyway -- a 4B model asked nicely for JSON returns fenced Markdown about
        a third of the time.
        """
        model = model or CHAT_MODEL
        if not model:
            raise LMStudioDown(
                "no chat model configured: set BYTECAT_CHAT_MODEL to a model your "
                "server has loaded (see `bytecat status` or your server's /v1/models)")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "atlas_label", "strict": True,
                                "schema": schema},
            }
        out = self._post("/chat/completions", payload)
        return reply_text(out["choices"][0]["message"])
