"""Minimal Chroma v2 client over stdlib urllib.

Deliberately not the `chromadb` package: we only need upsert and query, the
server already runs on :8100, and the same local-only invariant applies here as
to the model endpoint.

Chroma holds vectors and a little metadata for filtering. It is derived state --
if this server disappears, `embed.py` rebuilds it from the catalog.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

DEFAULT_BASE = "http://127.0.0.1:8100"
TENANT = "default_tenant"
DATABASE = "default_database"
COLLECTION = "bytecat_files"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _assert_local(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise RuntimeError(f"refusing non-loopback Chroma endpoint {host!r}")


class _LocalOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Vectors are derived from file content; the same guard applies here as to
    the model endpoint. See the longer note in llm.py."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_local(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_LocalOnlyRedirects)


class Chroma:
    def __init__(self, base: str = "", collection: str = ""):
        self.base = (base or os.environ.get("BYTECAT_CHROMA_BASE") or DEFAULT_BASE).rstrip("/")
        _assert_local(self.base)
        # Overridable so the test suite never writes into the real collection.
        self.collection_name = (
            collection or os.environ.get("BYTECAT_CHROMA_COLLECTION") or COLLECTION
        )
        self._id: Optional[str] = None

    # -- plumbing ---------------------------------------------------------

    def _req(self, method: str, path: str, payload: Any = None) -> Any:
        _assert_local(self.base)
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(req, timeout=60) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"chroma {method} {path} -> {e.code}: "
                               f"{e.read()[:300]!r}") from e

    @property
    def _root(self) -> str:
        return f"/api/v2/tenants/{TENANT}/databases/{DATABASE}/collections"

    def collection_id(self) -> str:
        """Get or create the collection. Idempotent, so a rebuild just works."""
        if self._id:
            return self._id
        out = self._req("POST", self._root, {
            "name": self.collection_name,
            "configuration": {"hnsw": {"space": "cosine"}},
            "get_or_create": True,
        })
        self._id = out["id"]
        return self._id

    def heartbeat(self) -> bool:
        try:
            self._req("GET", "/api/v2/heartbeat")
            return True
        except Exception:
            return False

    # -- data -------------------------------------------------------------

    def upsert(self, ids: list[str], embeddings: list[list[float]],
               metadatas: list[dict], documents: Optional[list[str]] = None) -> None:
        """Store vectors.

        `documents` is left None on purpose: Chroma would otherwise persist the
        raw extracted text of every personal file in its own store, which is
        exactly the copy of your data this project is trying not to make. The
        vector plus the catalog row is enough to find the file again.
        """
        cid = self.collection_id()
        self._req("POST", f"{self._root}/{cid}/upsert", {
            "ids": ids,
            "embeddings": embeddings,
            "metadatas": metadatas,
        })

    def query(self, embedding: list[float], n: int = 20,
              where: Optional[dict] = None) -> list[tuple[str, float, dict]]:
        cid = self.collection_id()
        payload: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": n,
            "include": ["metadatas", "distances"],
        }
        if where:
            payload["where"] = where
        out = self._req("POST", f"{self._root}/{cid}/query", payload)
        ids = (out.get("ids") or [[]])[0]
        dists = (out.get("distances") or [[]])[0]
        metas = (out.get("metadatas") or [[]])[0]
        return list(zip(ids, dists, metas))

    def count(self) -> int:
        cid = self.collection_id()
        return self._req("GET", f"{self._root}/{cid}/count") or 0

    def delete_collection(self) -> None:
        self._req("DELETE", f"{self._root}/{self.collection_name}")
        self._id = None
