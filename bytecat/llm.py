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
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .localonly import LocalOnlyError, assert_local, opener

DEFAULT_BASE = "http://127.0.0.1:1234/v1"

EMBED_MODEL = os.environ.get("BYTECAT_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
# The chat model used by tier 4 (`enrich`) and `ask --smart`. There is no
# default: set BYTECAT_CHAT_MODEL to whatever your server has loaded.
# Nothing checks it against the server's list; a stale name fails on the first
# chat request.
CHAT_MODEL = os.environ.get("BYTECAT_CHAT_MODEL", "")
# Set BYTECAT_LLM_AUTOSTART=1 to let `ensure_up()` run `lms server start`
# (LM Studio only). Off by default: a public tool should not launch apps.
AUTOSTART = os.environ.get("BYTECAT_LLM_AUTOSTART", "") == "1"


class LMStudioDown(RuntimeError):
    pass


def failure_reason(e: BaseException) -> str:
    """What to print when a model or Chroma call fails.

    bytecat's own errors (LocalOnlyError, LMStudioDown and the RuntimeErrors
    raised here and in chroma.py) carry a message that names the cause and the
    fix. Anything else -- a KeyError or JSONDecodeError from a malformed reply --
    prints its class name, because str(KeyError('data')) is just "'data'".
    """
    if isinstance(e, RuntimeError) and str(e):
        return str(e)
    return type(e).__name__


_NOTED: set[str] = set()


def note_once(line: str) -> None:
    """Print a fallback notice to stderr once per process: `eval` runs many
    searches, and one reason repeated per query buries the scoreboard."""
    if line not in _NOTED:
        _NOTED.add(line)
        print(line, file=sys.stderr)


# Every request in this module goes through here; see localonly.py.
_OPENER = opener("model")


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
        assert_local(self.base, "model")
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------

    def _post(self, path: str, payload: dict, *, retries: int = 3) -> dict:
        assert_local(self.base, "model")
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
                detail = e.read()[:300].decode(errors="replace")
                raise RuntimeError(f"model server {e.code} on {path}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                # A model being JIT-loaded can take a while on the first call.
                time.sleep(2 * (attempt + 1))

        raise LMStudioDown(f"{url} unreachable: {last}")

    # -- lifecycle --------------------------------------------------------

    def models(self) -> list[str]:
        assert_local(self.base, "model")
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
        # "or model": models() also returns [] for a running server that lists
        # no models, so the message cannot tell the two apart.
        if not autostart:
            raise LMStudioDown(
                f"no model server or model at {self.base}; "
                f"start one or set BYTECAT_LLM_BASE")

        lms = os.path.expanduser("~/.lmstudio/bin/lms")
        if not os.path.exists(lms):
            raise LMStudioDown(f"no model server or model at {self.base}; {lms} not found")
        r = subprocess.run([lms, "server", "start"], capture_output=True, timeout=60)
        for _ in range(10):
            time.sleep(2)
            models = self.models()
            if models:
                return models
        err = (r.stderr or b"").decode(errors="replace").strip()
        raise LMStudioDown(
            f"still no model server or model at {self.base} after {lms} server start"
            + (f": {err}" if err else "")
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

    def chat(self, system: str, user: str | list[dict], *, model: str = "",
             max_tokens: int = 300, temperature: float = 0.1,
             schema: Optional[dict] = None) -> str:
        """Chat completion, optionally constrained to a JSON schema.

        LM Studio implements structured output as `json_schema` and rejects
        OpenAI's older `json_object` form outright. Constraining beats hinting
        anyway -- a 4B model asked nicely for JSON returns fenced Markdown about
        a third of the time.

        `user` is text, or a list of OpenAI content parts (text plus an image
        for a vision model).
        """
        model = model or CHAT_MODEL
        if not model:
            raise LMStudioDown(f"BYTECAT_CHAT_MODEL not set; pick one from {self.base}/models")
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
                "json_schema": {"name": "bytecat_label", "strict": True,
                                "schema": schema},
            }
        out = self._post("/chat/completions", payload)
        return reply_text(out["choices"][0]["message"])
