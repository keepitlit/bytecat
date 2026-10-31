#!/usr/bin/env python3
"""Tier 4: local LLM titles and one-line gists. Budgeted, opt-in, resumable.

This is the only expensive stage, so it is also the only one that refuses to run
unbounded. A million files through an 8B model is not a thing anyone should
start by accident, so --budget is required to have a value and defaults small.

Files are ranked by how little we already know about them. A photo with clean
EXIF sitting in "2024/Italy" does not need a language model to be findable; a
file called "Scan_0043.pdf" in "Downloads" does. Spending the budget on the
second group is the entire idea.

Everything runs against the model server on loopback -- see llm.py for the invariant.

Usage:
    enrich.py --budget 200
    enrich.py --budget 500 --kinds document
    enrich.py --budget 100 --vision       # images, via the multimodal model
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from typing import Optional

from . import db, extract
from .llm import CHAT_MODEL, ModelServer, ModelServerDown

# Names that carry no information -- camera dumps, scanner output, OS defaults.
GENERIC_NAME = re.compile(
    r"^(img|dsc|dscn|pxl|photo|image|scan|scanned|doc|document|file|untitled|"
    r"new|copy|screen ?shot|screenshot|video|mov|vid|record(ing)?|export|"
    r"download|attachment|unnamed|final|draft|temp|test)"
    r"[\s_\-.]*\d*(\s*\(\d+\))?$",
    re.IGNORECASE,
)

SYSTEM = (
    "You label files for a personal archive index. You are given a file's path "
    "and a short excerpt. Reply with JSON only: "
    '{"title": "...", "gist": "...", "category": "..."}. '
    "title: a specific human name for this file, max 8 words. "
    "gist: one factual sentence about what it contains, max 25 words. "
    "category: one of finance, legal, medical, work, personal, photo, travel, "
    "reference, receipt, correspondence, creative, technical, admin, other. "
    "Use only what you are shown. Never invent names, dates, or amounts."
)

VISION_SYSTEM = (
    "You label images for a personal photo index. Reply with JSON only: "
    '{"title": "...", "gist": "...", "category": "..."}. '
    "title: what the image shows, max 8 words. "
    "gist: one factual sentence, max 25 words. "
    "category: one of photo, screenshot, document-scan, artwork, receipt, "
    "diagram, meme, other. Describe only what is visibly present."
)

EXCERPT_CHARS = 2500
VISION_MAX_BYTES = 4_000_000   # skip enormous images; resizing is not worth it here

# Constrains the model's output rather than asking politely for JSON. Small
# models otherwise return fenced Markdown a good fraction of the time.
LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "gist": {"type": "string"},
        "category": {"type": "string"},
    },
    "required": ["title", "gist", "category"],
}


def ambiguity_score(name: str, relpath: str, title: Optional[str],
                    captured: Optional[str], text_chars: Optional[int]) -> float:
    """Higher = we know less = better use of the budget."""
    score = 0.0
    stem = os.path.splitext(name)[0]

    if not title:
        score += 3.0
    if GENERIC_NAME.match(stem):
        score += 4.0
    if len(stem) <= 3:
        score += 1.5
    # A file adrift in a dumping ground tells us nothing from its location.
    lowered = relpath.lower()
    if any(d in lowered for d in ("download", "desktop", "inbox", "unsorted",
                                  "untitled", "misc", "temp", "new folder")):
        score += 2.0
    if relpath.count(os.sep) <= 1:
        score += 0.5
    if not captured:
        score += 0.5
    if text_chars and text_chars > 200:
        score += 1.0     # there is real content to summarise
    return score


def candidates(conn, kinds: Optional[list[str]], budget: int,
               vision: bool) -> list[dict]:
    q = """SELECT f.id, f.relpath, f.name, f.ext, f.size_bytes, v.last_mount,
                  e.title, e.captured_at, e.text_chars, e.kind
           FROM files f
           JOIN volumes v ON v.id = f.volume_id
           LEFT JOIN extractions e ON e.file_id = f.id
           WHERE """ + db.behind("t4_enriched")
    params: list = []
    if kinds:
        q += f" AND COALESCE(e.kind,'other') IN ({','.join('?' * len(kinds))})"
        params += kinds
    elif vision:
        q += " AND COALESCE(e.kind,'other') = 'image'"
    else:
        # 'code' is in here because db.KIND_BY_EXT maps html to code, which left
        # saved web pages and exported reports permanently unlabelled. Widening
        # the filter is the right fix rather than reclassifying html as a
        # document -- that would quietly break `ask --kind code`. A labelled
        # script earns its place in a personal archive anyway: "backs up the
        # photo library to the NAS" is a better index entry than "a .sh file".
        q += (" AND COALESCE(e.kind,'other') IN "
              "('document','data','other','code')")

    rows = [{
        "id": r["id"], "path": p, "relpath": r["relpath"], "name": r["name"],
        "ext": r["ext"], "size": r["size_bytes"], "kind": r["kind"] or "other",
        "score": ambiguity_score(r["name"], r["relpath"], r["title"],
                                 r["captured_at"], r["text_chars"]),
    } for r, p in db.on_disk(conn.execute(q, params))]

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:budget]


def parse_reply(raw: str) -> Optional[dict]:
    """Small models wrap JSON in prose or fences often enough to matter."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(d, dict):
        return None
    return {
        "title": str(d.get("title", ""))[:200] or None,
        "gist": str(d.get("gist", ""))[:300] or None,
        "category": str(d.get("category", ""))[:50].lower() or None,
    }


def enrich_text(lm: ModelServer, row: dict) -> Optional[dict]:
    body = extract.extract_text(row["path"], row["ext"], row["size"]) or ""
    body = " ".join(body.split())[:EXCERPT_CHARS]
    user = f"path: {row['relpath']}\nfilename: {row['name']}\n\nexcerpt:\n{body}"
    return parse_reply(lm.chat(SYSTEM, user, max_tokens=220, schema=LABEL_SCHEMA))


def enrich_image(lm: ModelServer, row: dict) -> Optional[dict]:
    """Multimodal path. Requires CHAT_MODEL to be vision-capable; a text-only
    model returns an error and the row falls back to path + EXIF."""
    if row["size"] > VISION_MAX_BYTES:
        return None
    try:
        with open(row["path"], "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
    except OSError:
        return None

    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
            "gif": "gif", "webp": "webp"}.get((row["ext"] or "").lower())
    if not mime:
        return None

    content = [
        {"type": "text", "text": f"path: {row['relpath']}"},
        {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
    ]
    return parse_reply(lm.chat(VISION_SYSTEM, content, max_tokens=200, schema=LABEL_SCHEMA))


def run(conn, *, budget: int, kinds: Optional[list[str]], vision: bool) -> dict:
    lm = ModelServer()
    lm.ensure_up()
    if not CHAT_MODEL:
        raise ModelServerDown("set BYTECAT_CHAT_MODEL to a model your server has loaded")

    rows = candidates(conn, kinds, budget, vision)
    stats = {"seen": len(rows), "enriched": 0, "failed": 0}
    if not rows:
        print("[bytecat] nothing to enrich", file=sys.stderr)
        return stats

    run_id = db.start_run(conn, "enrich")
    print(f"[bytecat] enriching {len(rows):,} files", file=sys.stderr)

    for i, row in enumerate(rows, 1):
        try:
            result = enrich_image(lm, row) if vision else enrich_text(lm, row)
        except ModelServerDown:
            # Rows done so far are already marked, so a re-run continues.
            print(f"[bytecat] model server at {lm.base} stopped responding; "
                  f"re-run to continue", file=sys.stderr)
            break
        except Exception as e:
            with db.transaction(conn):
                db.log_error(conn, row["id"], "enrich", e)
            result = None

        with db.transaction(conn):
            if result and (result["title"] or result["gist"]):
                extract.upsert_extraction(
                    conn, row["id"],
                    title=result["title"], gist=result["gist"],
                    category=result["category"], source="llm", confidence=0.6,
                )
                # A title and gist are new signal, and the file's existing
                # vector predates them. Send it back through tier 3 so the
                # semantic index reflects what we now know.
                conn.execute(
                    "UPDATE files SET t3_embedded = 0 WHERE id = ?", (row["id"],)
                )
                stats["enriched"] += 1
            else:
                stats["failed"] += 1
            # Marked either way: a file that cannot be described should not be
            # retried on every future run.
            conn.execute("UPDATE files SET t4_enriched = 1 WHERE id = ?", (row["id"],))

        if i % 10 == 0:
            print(f"[bytecat] {i:,}/{len(rows):,}", file=sys.stderr)

    db.fts_sync(conn, [r["id"] for r in rows])
    db.finish_run(conn, run_id, status="ok", n_seen=stats["seen"],
                  n_changed=stats["enriched"], n_errors=stats["failed"])
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(prog="bytecat enrich",
        description="label files with a local model (tier 4)")
    ap.add_argument("--budget", type=int, default=200,
                    help="at most N files (default 200)")
    ap.add_argument("--kinds", default="",
                    help="only these kinds, comma-separated (default: document,data,other,code)")
    ap.add_argument("--vision", action="store_true",
                    help="label images; BYTECAT_CHAT_MODEL must accept images")
    args = ap.parse_args()

    if args.budget <= 0:
        print("[bytecat] --budget must be positive", file=sys.stderr)
        return 2

    conn = db.open_catalog()
    try:
        s = run(conn, budget=args.budget,
                kinds=[k for k in args.kinds.split(",") if k] or None,
                vision=args.vision)
    except RuntimeError as e:
        # ModelServerDown and LocalOnlyError name what failed and what to do; print
        # that line instead of a traceback. The exit code stays 1.
        print(f"[bytecat] {e}", file=sys.stderr)
        return 1
    # Counted from the files actually tried: a stop part-way leaves the rest
    # untried, and they are not failures.
    if s["enriched"] or s["failed"]:
        nxt = "; run bytecat embed next" if s["enriched"] else ""
        print(f"[bytecat] labelled {s['enriched']:,}; "
              f"{s['failed']:,} got no label{nxt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
