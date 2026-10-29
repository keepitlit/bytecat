#!/usr/bin/env python3
"""Tier 2 (metadata) and the text half of tier 3. No LLM involved.

Most files can be described well without a language model: EXIF knows when and
on what a photo was taken, ID3 knows the artist, a PDF often carries a real
title. Doing this first is what keeps the expensive tier small.

Extracted text is returned to the caller and never persisted. What lands in the
catalog is a length, a checksum, and short derived fields -- the text itself is
embedded and dropped.

Usage:
    extract.py                 # tier-2 metadata pass over everything pending
    extract.py --limit 500
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from typing import Iterator, Optional

from . import db

# Tags worth keeping. exiftool emits hundreds per file; storing them all would
# bloat the catalog with lens-correction matrices nobody will ever search.
EXIF_TAGS = [
    "DateTimeOriginal", "CreateDate", "ModifyDate", "GPSLatitude", "GPSLongitude",
    "Make", "Model", "LensModel", "ImageWidth", "ImageHeight", "Orientation",
    "Title", "Subject", "Keywords", "Description", "Caption-Abstract",
    "Author", "Creator", "Artist", "Album", "Genre", "Year", "Track",
    "Duration", "MediaDuration", "VideoFrameRate", "AudioChannels",
    "PageCount", "Producer", "Company", "LastModifiedBy",
]

EXIF_BATCH = 200
TEXT_TIMEOUT = 60          # a single pathological PDF must not wedge the pass
MAX_TEXT_BYTES = 200_000   # embedding context is finite; the head is the useful part

MEDIA_KINDS = {"image", "video", "audio"}
TEXT_KINDS = {"document", "code", "data"}


def has_exiftool() -> bool:
    return subprocess.run(["which", "exiftool"], capture_output=True).returncode == 0


# --------------------------------------------------------------------------
# tier 2 -- metadata
# --------------------------------------------------------------------------

def exif_batch(paths: list[str]) -> dict[str, dict]:
    """Run exiftool over many files at once and return {path: metadata}.

    Batched because exiftool's startup cost dwarfs its per-file cost; one call
    per file would make this stage roughly an order of magnitude slower.
    """
    if not paths:
        return {}
    args = ["exiftool", "-j", "-n", "-fast2", "-m", "-q"]
    args += [f"-{t}" for t in EXIF_TAGS]
    args += paths
    try:
        out = subprocess.run(args, capture_output=True, timeout=180).stdout
        records = json.loads(out or b"[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}

    result = {}
    for rec in records:
        src = rec.pop("SourceFile", None)
        if src:
            result[src] = {k: v for k, v in rec.items() if v not in (None, "")}
    return result


def captured_at(meta: dict) -> Optional[str]:
    """Best-effort content date. EXIF beats filesystem mtime every time --
    mtime says when the file was copied, not when the photo was taken."""
    for key in ("DateTimeOriginal", "CreateDate", "ModifyDate", "Year"):
        v = meta.get(key)
        if not v:
            continue
        s = str(v)
        # exiftool's native form is "2024:06:01 14:23:11"
        if len(s) >= 10 and s[4] == ":" and s[7] == ":":
            return s[:10].replace(":", "-") + s[10:19]
        if len(s) == 4 and s.isdigit():
            return f"{s}-01-01"
    return None


def title_from_meta(meta: dict) -> Optional[str]:
    for key in ("Title", "Description", "Caption-Abstract", "Subject"):
        v = meta.get(key)
        if v and isinstance(v, str) and len(v.strip()) > 2:
            return v.strip()[:200]
    return None


# --------------------------------------------------------------------------
# tier 3 -- text
# --------------------------------------------------------------------------

def markitdown_text(path: str) -> Optional[str]:
    """Convert a document to Markdown via the already-installed markitdown.

    Reused rather than reimplemented: it already handles PDF, Office, EPUB,
    HTML and audio transcription, which is most of what a personal archive is.
    Run as a subprocess so a malformed file segfaults a child, not the pass.
    """
    try:
        r = subprocess.run(
            ["markitdown", path],
            capture_output=True, timeout=TEXT_TIMEOUT,
        )
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace")[:MAX_TEXT_BYTES]
    except (subprocess.TimeoutExpired, OSError):
        return None


def plain_text(path: str, size: int) -> Optional[str]:
    if size > MAX_TEXT_BYTES * 4:
        # Read only the head of very large text files.
        try:
            with open(path, "rb") as fh:
                return fh.read(MAX_TEXT_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return None
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_TEXT_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


PLAIN_EXTS = {
    "txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "toml", "xml",
    "log", "ndjson", "py", "js", "ts", "tsx", "jsx", "go", "rs", "rb", "java",
    "kt", "swift", "c", "h", "cpp", "hpp", "cs", "php", "sh", "zsh", "sql",
    "html", "css", "scss", "vue", "svelte", "lua", "pl", "r",
}


# Extensions markitdown is allowed to see. An ALLOWLIST, deliberately.
#
# markitdown is a subprocess with its own network stack and it does not share
# bytecat's local-only invariant. Its audio converter accepts .wav/.mp3/.m4a/.mp4
# and transcribes them through speech_recognition's `recognize_google()`, which
# POSTs the raw audio to http://www.google.com/speech-api/v2/recognize -- in
# cleartext, to a third party. The catch-all this replaced ("anything that isn't
# plain text goes to markitdown") therefore uploaded every voice memo, recorded
# call and home video on the drive.
#
# Never turn this back into a fallback. Adding an extension here is an assertion
# that markitdown handles it without the bytes leaving the machine.
MARKITDOWN_EXTS = {
    "pdf", "doc", "docx", "odt", "rtf", "pages", "tex",
    "epub", "mobi", "azw3",
    "ppt", "pptx", "key", "odp",
    "xls", "xlsx", "numbers", "ods",
}

# Refused outright, and named rather than merely omitted so the reason survives.
# Redundant against the allowlist above on purpose -- this is the belt to its
# braces, and it is what the regression test pins.
NEVER_EXTRACT_EXTS = {
    "mp3", "m4a", "aac", "flac", "wav", "aiff", "ogg", "opus", "wma",
    "mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv", "mpg", "mpeg", "3gp",
}


def extract_text(path: str, ext: str, size: int) -> Optional[str]:
    """Get text for embedding. Returns None when there is nothing useful.

    Anything not explicitly allowed returns None rather than falling through to
    markitdown. A file whose body we skip is still indexed by path, name and
    tier-2 metadata: losing a body is cheap, leaking one is not.
    """
    ext = (ext or "").lower().lstrip(".")
    if ext in NEVER_EXTRACT_EXTS:
        return None
    if ext in PLAIN_EXTS:
        return plain_text(path, size)
    if ext in MARKITDOWN_EXTS:
        return markitdown_text(path)
    return None


def text_digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).hexdigest()


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def pending(conn, stage_col: str, limit: int = 0) -> list[dict]:
    """Files still behind on a tier, with a resolvable local path."""
    q = f"""SELECT f.id, f.relpath, f.ext, f.size_bytes, v.last_mount
            FROM files f JOIN volumes v ON v.id = f.volume_id
            WHERE f.{stage_col} = 0 AND f.state = 'present'
              AND f.is_symlink = 0 AND v.state = 'online'
            ORDER BY f.id"""
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = []
    for r in conn.execute(q):
        mount = r["last_mount"]
        if not mount:
            continue
        p = os.path.join(mount, r["relpath"])
        if os.path.exists(p):
            rows.append({"id": r["id"], "path": p, "ext": r["ext"],
                         "size": r["size_bytes"]})
    return rows


def upsert_extraction(conn, file_id: int, **fields) -> None:
    cols = ", ".join(fields)
    ph = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
    conn.execute(
        f"""INSERT INTO extractions (file_id, {cols}) VALUES (?, {ph})
            ON CONFLICT(file_id) DO UPDATE SET {updates},
                                               updated_at = datetime('now')""",
        (file_id, *fields.values()),
    )


def run_tier2(conn, limit: int = 0) -> dict:
    rows = pending(conn, "t2_meta", limit)
    stats = {"seen": len(rows), "meta": 0, "errors": 0}
    if not rows:
        return stats

    run_id = db.start_run(conn, "extract")
    use_exif = has_exiftool()

    by_id = {r["id"]: r for r in rows}
    media = [r for r in rows if db.kind_for_ext(r["ext"]) in MEDIA_KINDS]
    others = [r for r in rows if db.kind_for_ext(r["ext"]) not in MEDIA_KINDS]

    # Media: batched exiftool.
    for i in range(0, len(media), EXIF_BATCH):
        chunk = media[i:i + EXIF_BATCH]
        got = exif_batch([r["path"] for r in chunk]) if use_exif else {}
        with db.transaction(conn):
            for r in chunk:
                meta = got.get(r["path"], {})
                upsert_extraction(
                    conn, r["id"],
                    kind=db.kind_for_ext(r["ext"]),
                    meta_json=json.dumps(meta) if meta else None,
                    captured_at=captured_at(meta),
                    title=title_from_meta(meta),
                    source="exif" if meta else "heuristic",
                )
                conn.execute("UPDATE files SET t2_meta = 1 WHERE id = ?", (r["id"],))
                stats["meta"] += 1
        print(f"[bytecat] media {min(i+EXIF_BATCH, len(media)):,}/{len(media):,}",
              file=sys.stderr)

    # Everything else: record the coarse kind so tier 3 knows what to do.
    with db.transaction(conn):
        for r in others:
            upsert_extraction(conn, r["id"], kind=db.kind_for_ext(r["ext"]),
                              source="heuristic")
            conn.execute("UPDATE files SET t2_meta = 1 WHERE id = ?", (r["id"],))
            stats["meta"] += 1

    db.finish_run(conn, run_id, status="ok", n_seen=stats["seen"],
                  n_changed=stats["meta"], n_errors=stats["errors"])
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(prog="bytecat extract",
        description="read media metadata and file types (tier 2)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = db.open_catalog()
    if not has_exiftool():
        print("[bytecat] warning: exiftool not found; media metadata will be empty",
              file=sys.stderr)
    s = run_tier2(conn, args.limit)
    # No error count: nothing in this pass increments one.
    print(f"[bytecat] described {s['meta']:,} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
