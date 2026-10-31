#!/usr/bin/env python3
"""Tier 3: extract text, embed locally, store the vector. Resumable.

Text is extracted, embedded, and discarded in the same breath -- it is never
written to the catalog or to Chroma. What persists is a 768-dim vector, a
character count, and a checksum that lets a re-run skip unchanged files.

Runs against the local model server on loopback. If the server is down the stage stops with
an error rather than degrading to anything remote.

Usage:
    embed.py --limit 500
    embed.py --kinds document          # documents only
    embed.py --reset                   # rebuild vectors from scratch
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from . import db, extract
from .chroma import Chroma
from .llm import ModelServer, ModelServerDown

BATCH = 32                  # embedding requests per call
MAX_EMBED_CHARS = 8000      # nomic-embed's useful context


def build_text(path: str, name: str, relpath: str, ext: str, size: int,
               meta_title: Optional[str], gist: Optional[str] = None,
               category: Optional[str] = None) -> Optional[str]:
    """Assemble what actually gets embedded.

    The path is included alongside the content on purpose: folder and file names
    carry real signal in a personal archive ("taxes/2024/return.pdf" is often
    more informative than page one of the PDF), and it means a file whose text
    extraction fails is still findable.

    The tier-4 gist is included for a different reason: it is a paraphrase. Raw
    document text answers a query that shares its vocabulary, but "money I am
    owed" shares no words with a reimbursement form -- while it does share
    meaning with a sentence describing one. Enrichment already resets
    t3_embedded so labelled files come back through here; before this the gist
    reached FTS and nothing else, so half of what tier 4 produced was unused by
    the ranker that needed it most.
    """
    parts = [f"path: {relpath}", f"name: {name}"]
    if meta_title:
        parts.append(f"title: {meta_title}")
    if gist:
        parts.append(f"summary: {gist}")
    if category:
        parts.append(f"category: {category}")

    # Every kind is embedded, media included -- that is what makes
    # `ask --kind image` work, since a path is often the only description a
    # photo has. Media is safe to pass through here because extract_text gates
    # on an allowlist and returns None for it rather than shelling out.
    body = extract.extract_text(path, ext, size)
    if body:
        body = " ".join(body.split())          # collapse whitespace
        parts.append(body[:MAX_EMBED_CHARS])
    elif not meta_title:
        # Nothing but the path; still worth indexing, but flag it as thin.
        pass

    text = "\n".join(parts)
    return text if text.strip() else None


def pending(conn, kinds: Optional[list[str]], limit: int) -> list[dict]:
    q = """SELECT f.id, f.relpath, f.name, f.ext, f.size_bytes,
                  v.last_mount, e.title, e.gist, e.category, e.kind
           FROM files f
           JOIN volumes v ON v.id = f.volume_id
           LEFT JOIN extractions e ON e.file_id = f.id
           WHERE """ + db.behind("t3_embedded")
    params: list = []
    if kinds:
        q += f" AND COALESCE(e.kind, 'other') IN ({','.join('?' * len(kinds))})"
        params += kinds
    q += " ORDER BY f.id"
    if limit:
        q += f" LIMIT {int(limit)}"

    return [{
        "id": r["id"], "path": p, "relpath": r["relpath"],
        "name": r["name"], "ext": r["ext"], "size": r["size_bytes"],
        "title": r["title"], "gist": r["gist"],
        "category": r["category"], "kind": r["kind"] or "other",
    } for r, p in db.on_disk(conn.execute(q, params))]


def run(conn, *, kinds: Optional[list[str]] = None, limit: int = 0) -> dict:
    lm = ModelServer()
    lm.ensure_up()
    chroma = Chroma()
    if not chroma.heartbeat():
        raise RuntimeError(f"Chroma not reachable at {chroma.base}; "
                           f"start it or set BYTECAT_CHROMA_BASE")

    rows = pending(conn, kinds, limit)
    stats = {"seen": len(rows), "embedded": 0, "skipped": 0, "errors": 0}
    if not rows:
        print("[bytecat] nothing to embed", file=sys.stderr)
        return stats

    run_id = db.start_run(conn, "embed")
    print(f"[bytecat] embedding {len(rows):,} files", file=sys.stderr)

    buf: list[dict] = []

    def flush() -> None:
        if not buf:
            return
        try:
            vectors = lm.embed([b["text"] for b in buf])
        except ModelServerDown:
            raise
        except Exception as e:
            with db.transaction(conn):
                for b in buf:
                    db.log_error(conn, b["id"], "embed", e)
            stats["errors"] += len(buf)
            buf.clear()
            return

        chroma.upsert(
            ids=[str(b["id"]) for b in buf],
            embeddings=vectors,
            metadatas=[{
                "file_id": b["id"],
                "kind": b["kind"],
                "ext": b["ext"] or "",
                "size": b["size"],
            } for b in buf],
        )

        with db.transaction(conn):
            for b in buf:
                extract.upsert_extraction(
                    conn, b["id"],
                    kind=b["kind"],
                    text_chars=b["chars"],
                    text_sha=b["sha"],
                )
                conn.execute("UPDATE files SET t3_embedded = 1 WHERE id = ?", (b["id"],))
        stats["embedded"] += len(buf)
        buf.clear()

    for i, r in enumerate(rows, 1):
        try:
            text = build_text(r["path"], r["name"], r["relpath"],
                              r["ext"], r["size"], r["title"],
                              r["gist"], r["category"])
        except Exception as e:
            with db.transaction(conn):
                db.log_error(conn, r["id"], "embed", e)
                conn.execute("UPDATE files SET t3_embedded = 1 WHERE id = ?", (r["id"],))
            stats["errors"] += 1
            continue

        if not text:
            with db.transaction(conn):
                conn.execute("UPDATE files SET t3_embedded = 1 WHERE id = ?", (r["id"],))
            stats["skipped"] += 1
            continue

        buf.append({**r, "text": text, "chars": len(text),
                    "sha": extract.text_digest(text)})
        if len(buf) >= BATCH:
            flush()
            print(f"[bytecat] {i:,}/{len(rows):,}", file=sys.stderr)

    flush()
    db.fts_sync(conn, [r["id"] for r in rows])
    db.finish_run(conn, run_id, status="ok", n_seen=stats["seen"],
                  n_changed=stats["embedded"], n_errors=stats["errors"])
    return stats


def unmounted_files(conn) -> int:
    """Present files on drives not mounted here, this host's or another's;
    pending() skips them."""
    here, states = db.this_host(), db.volume_states(conn)
    n = 0
    for v in conn.execute("""SELECT v.id, h.name AS host, COUNT(f.id) AS n
                             FROM volumes v JOIN files f ON f.volume_id = v.id
                             JOIN hosts h ON h.id = v.host_id
                             WHERE f.state = 'present' GROUP BY v.id"""):
        if v["host"] != here or states.get(v["id"]) != "online":
            n += v["n"]
    return n


def main() -> int:
    ap = argparse.ArgumentParser(prog="bytecat embed",
        description="index files for semantic search with a local model (tier 3)")
    ap.add_argument("--limit", type=int, default=0, help="embed at most N files")
    ap.add_argument("--kinds", default="",
                    help="only these kinds, comma-separated (document,image,...)")
    ap.add_argument("--reset", action="store_true",
                    help="delete all vectors, then re-embed files on mounted volumes")
    args = ap.parse_args()

    conn = db.open_catalog()
    try:
        if args.reset:
            Chroma().delete_collection()
            conn.execute("UPDATE files SET t3_embedded = 0")
            n = unmounted_files(conn)
            left = f"; {n:,} files on unmounted volumes not re-embedded" if n else ""
            print(f"[bytecat] vectors cleared{left}")

        s = run(conn, kinds=[k for k in args.kinds.split(",") if k] or None,
                limit=args.limit)
    except RuntimeError as e:
        # ModelServerDown, LocalOnlyError and the Chroma errors each name what
        # failed and what to do; print that line instead of a traceback. The
        # exit code stays 1, as for the uncaught exception.
        print(f"[bytecat] {e}", file=sys.stderr)
        return 1
    # No skipped count: build_text always has the path and name, so it never
    # comes back empty.
    if s["seen"]:
        print(f"[bytecat] embedded {s['embedded']:,}; {s['errors']:,} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
