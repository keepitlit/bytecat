#!/usr/bin/env python3
"""Hybrid search over the catalog: FTS5 keyword + local vector similarity.

Neither half is sufficient alone. Keyword search nails "the file literally
called invoice_2024" and fails on "that insurance thing"; vectors do the
opposite. Results are fused with Reciprocal Rank Fusion, which needs no score
calibration between two very differently-scaled rankers.

Degrades rather than fails: with Chroma down you still get keyword results, and
with LM Studio down you still get keyword results.

Output is paths and metadata. Never file content -- that is the whole point of
building this locally, and it also keeps results safe to paste into a session.

Usage:
    ask.py "insurance policy from 2024"
    ask.py "photos from the cabin trip" --kind image --limit 30
    ask.py "tax return" --host nas --include-offline
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional

from . import db
from .chroma import Chroma
from .rerank import rerank
from .llm import LMStudio, LMStudioDown

RRF_K = 60  # standard damping constant; larger = flatter contribution by rank


# Dropped from keyword queries. People ask questions in sentences, and an
# OR-ed "the" matches most of the corpus -- which drowns the one token that
# actually mattered. The vector half handles phrasing; FTS only needs content words.
STOPWORDS = frozenset("""
a an the of for from with to in on at by and or is are was were be been being
my me i we our you your it its this that these those there here what which who
whom whose when where why how do does did done can could should would may might
about into over under again all any some no not only own same so than too very
find show get me all files file stuff thing things something anything
""".split())


def fts_query(raw: str) -> str:
    """Turn a natural-language question into a safe FTS5 MATCH expression.

    FTS5 treats a lot of punctuation as syntax, so an unescaped user string is
    both a crash risk and a source of baffling results. Each surviving token is
    quoted and OR-ed; ranking sorts out relevance.
    """
    tokens = [t for t in re.findall(r"[A-Za-z0-9_]+", raw.lower())
              if len(t) > 1 and t not in STOPWORDS]
    if not tokens:
        # An all-stopword query still deserves the vector half; returning ""
        # simply means keyword contributes nothing.
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def keyword_search(conn, query: str, limit: int) -> list[int]:
    expr = fts_query(query)
    if not expr:
        return []
    try:
        rows = conn.execute(
            """SELECT rowid FROM files_fts
               WHERE files_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (expr, limit),
        ).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def vector_search(query: str, limit: int) -> list[int]:
    try:
        lm = LMStudio()
        if not lm.models():
            # One fast probe. embed() retries with back-off for a model that is
            # still loading, which is right for the pipeline and wrong for an
            # interactive search against a server that is simply off.
            raise LMStudioDown(f"no model server at {lm.base}")
        vec = lm.embed([query])[0]
        hits = Chroma().query(vec, n=limit)
        return [int(i) for i, _dist, _meta in hits]
    except Exception as e:
        print(f"[bytecat] vector search unavailable ({type(e).__name__}); "
              f"keyword only", file=sys.stderr)
        return []


def rrf(rankings: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, fid in enumerate(ranking):
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def hydrate(conn, file_ids: list[int], *, kind: str = "", host: str = "",
            include_offline: bool = True, collapse: bool = True) -> list[dict]:
    if not file_ids:
        return []
    ph = ",".join("?" * len(file_ids))
    q = f"""SELECT f.id, f.relpath, f.name, f.ext, f.size_bytes, f.mtime, f.state,
                   f.blob_id,
                   v.last_mount, v.label AS vol_label, v.uuid AS vol_uuid,
                   v.state AS vol_state, h.name AS host,
                   e.kind, e.title, e.gist, e.captured_at,
                   b.n_files AS n_copies
            FROM files f
            JOIN volumes v ON v.id = f.volume_id
            JOIN hosts   h ON h.id = v.host_id
            LEFT JOIN extractions e ON e.file_id = f.id
            LEFT JOIN blobs b ON b.id = f.blob_id
            WHERE f.id IN ({ph}) AND f.state = 'present'"""
    params: list = list(file_ids)
    if kind:
        q += " AND e.kind = ?"
        params.append(kind)
    if host:
        q += " AND h.name = ?"
        params.append(host)
    if not include_offline:
        q += " AND v.state = 'online'"

    by_id = {r["id"]: dict(r) for r in conn.execute(q, params)}
    ordered = [by_id[i] for i in file_ids if i in by_id]
    if not collapse:
        return ordered

    # Identical content is one answer, not N. A single duplicated blob was
    # taking three separate slots in the top 30 on this corpus -- 45 copies of
    # the same 74-byte cache file exist, so without this a result page can be
    # filled by one piece of content. The best-ranked copy stands in for the
    # rest, and n_copies already tells the reader there are others.
    # A file with no blob_id has not been hashed yet, so it cannot be shown to
    # duplicate anything and is kept on its own.
    seen: set = set()
    out = []
    for r in ordered:
        key = ("blob", r["blob_id"]) if r["blob_id"] else ("file", r["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(x) < 1024 or unit == "TB":
            return f"{int(x)}{unit}" if unit == "B" else f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}TB"


def render(results: list[dict], *, show_full_path: bool) -> str:
    if not results:
        return "no matches"
    out = []
    for i, r in enumerate(results, 1):
        loc = f"{r['host']}:{r['vol_label'] or r['vol_uuid'][:8]}"
        offline = "" if r["vol_state"] == "online" else "  [volume offline]"
        dupes = f"  [{r['n_copies']} copies]" if (r["n_copies"] or 0) > 1 else ""
        when = (r["captured_at"] or "")[:10]

        path = os.path.join(r["last_mount"] or "", r["relpath"]) \
            if show_full_path and r["last_mount"] else r["relpath"]

        out.append(f"{i:2}. {r['name']}   {human_size(r['size_bytes'])}"
                   f"  {r['kind'] or '?'}{dupes}{offline}")
        out.append(f"    {loc}  {path}")
        if r.get("title"):
            out.append(f"    title: {r['title'][:110]}")
        if r.get("gist"):
            out.append(f"    {r['gist'][:110]}")
        if when:
            out.append(f"    dated: {when}")
        out.append("")
    return "\n".join(out)


def search(conn, query: str, *, limit: int = 20, kind: str = "", host: str = "",
           include_offline: bool = True, pool: int = 0,
           collapse: bool = True, smart: bool = False) -> list[dict]:
    """Run both rankers and fuse. `pool` is how deep each ranker goes.

    `smart` adds a local-LLM reranking pass over the fused pool. It is off by
    default because it turns a sub-second search into a multi-second one; that
    is a good trade when the answer is hard to phrase and a bad one to impose on
    every lookup.
    """
    pool = pool or max(limit * 4, 60)
    kw = keyword_search(conn, query, pool)
    vec = vector_search(query, pool)
    fused = rrf([r for r in (kw, vec) if r])
    ordered = [fid for fid, _ in fused]
    rows = hydrate(conn, ordered, kind=kind, host=host,
                   include_offline=include_offline, collapse=collapse)
    if smart:
        # Rerank before slicing: the whole point is that the right answer is
        # sitting below the cut line.
        rows = rerank(query, rows)
    return rows[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="search the bytecat catalog")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--kind", default="",
                    help="document|image|video|audio|archive|code|data|other")
    ap.add_argument("--host", default="")
    ap.add_argument("--online-only", action="store_true",
                    help="hide files on volumes that are not currently connected")
    ap.add_argument("--full-path", action="store_true")
    ap.add_argument("--smart", action="store_true",
                    help="rerank results with the local chat model (slower)")
    args = ap.parse_args()

    try:
        conn = db.connect(readonly=True)
    except db.NoCatalog as e:
        # Run as a child process by `bytecat ask` / `bytecat dupes`, so this never
        # reaches __main__.py. An empty catalog is a thing to be told.
        print(f"[bytecat] {e}", file=sys.stderr)
        return 2
    results = search(conn, " ".join(args.query), limit=args.limit,
                     kind=args.kind, host=args.host,
                     include_offline=not args.online_only, smart=args.smart)
    print(render(results, show_full_path=args.full_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
