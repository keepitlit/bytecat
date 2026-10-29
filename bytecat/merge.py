#!/usr/bin/env python3
"""Ingest scanner NDJSON into the catalog. Idempotent.

Re-running a host's scan updates rows rather than duplicating them, because
identity is (host, volume_uuid, relpath) and never the mount path.

Usage:
    scan.py ~/Documents | merge.py
    merge.py nas.ndjson
    ssh nas 'python3 - /mnt/user' < scan.py | merge.py
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterator, Optional, TextIO

from . import db


BATCH = 5000


def _records(stream: TextIO, stats: dict) -> Iterator[dict]:
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Counted, and reported once after the summary.
            stats["bad"] += 1


def merge_stream(conn, stream: TextIO, *, prune: bool = True) -> dict:
    """Consume NDJSON and apply it to the catalog.

    `prune` marks files that the scan did not see as 'missing'. It applies only
    to volumes actually present in this stream -- a volume that was offline and
    therefore absent is left completely untouched.
    """
    host_id: Optional[int] = None
    host_name = ""
    vol_id: Optional[int] = None
    vol_uuid = ""

    batch: list[tuple] = []
    stats = {"files": 0, "errors": 0, "volumes": 0, "missing": 0, "bad": 0}

    # Opened up front so every row this merge writes carries the same stamp;
    # deletion detection is "rows on this volume that this run did not touch".
    run_id = db.start_run(conn, "merge")

    def flush() -> None:
        if batch:
            db.upsert_files(conn, batch, run_id)
            batch.clear()

    def close_volume() -> None:
        """Finish the volume currently being read, pruning if asked."""
        nonlocal vol_id
        if vol_id is None:
            return
        flush()
        if prune:
            stats["missing"] += db.mark_missing(conn, vol_id, run_id)
        vol_id = None

    try:
        conn.execute("BEGIN")
        for rec in _records(stream, stats):
            kind = rec.get("type")

            if kind == "volume":
                close_volume()
                host_name = rec.get("host") or "unknown"
                host_id = db.upsert_host(conn, host_name, rec.get("platform", ""))
                vol_uuid = rec["uuid"]
                vol_id = db.upsert_volume(
                    conn, host_id, vol_uuid,
                    label=rec.get("label", "") or "",
                    fs_type=rec.get("fs_type", "") or "",
                    total_bytes=rec.get("total_bytes"),
                    mount=rec.get("root", ""),
                )
                stats["volumes"] += 1
                conn.execute(
                    "UPDATE runs SET host = ?, volume_uuid = ? WHERE id = ?",
                    (host_name, vol_uuid, run_id),
                )

            elif kind == "file":
                if vol_id is None:
                    continue  # file record before any volume header
                batch.append((
                    vol_id, rec["relpath"], rec["name"], rec.get("ext", ""),
                    rec["size"], rec["mtime"], rec.get("inode"),
                    rec.get("nlink", 1), rec.get("symlink", 0),
                ))
                stats["files"] += 1
                if len(batch) >= BATCH:
                    flush()

            elif kind == "error":
                stats["errors"] += 1

            elif kind == "summary":
                pass

        close_volume()
        # Keyword search must work from the first merge, before any model server
        # is involved: index the path and name of every row this run touched.
        # Rows that are new or renamed have no FTS entry yet; rows that already
        # carry a title/gist from enrichment keep them (fts_sync re-reads both).
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM files WHERE last_run = ? AND state = 'present'", (run_id,))]
        for i in range(0, len(ids), 5000):
            db.fts_sync(conn, ids[i:i + 5000])
        db.finish_run(conn, run_id, status="ok", n_seen=stats["files"],
                      n_changed=stats["missing"], n_errors=stats["errors"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(prog="bytecat merge",
                                 description="merge scanner NDJSON into the catalog")
    ap.add_argument("path", nargs="?", help="NDJSON file (default: stdin)")
    ap.add_argument("--no-prune", action="store_true",
                    help="do not mark unseen files as missing (for partial scans)")
    args = ap.parse_args()

    conn = db.open_catalog()
    stream = open(args.path) if args.path else sys.stdin
    try:
        stats = merge_stream(conn, stream, prune=not args.no_prune)
    finally:
        if args.path:
            stream.close()

    # Counts only -- never paths or content.
    if not stats["volumes"]:
        print("[bytecat] no scan output in input")
    else:
        # Under --no-prune nothing is marked missing, so the count is not drawn.
        missing = "" if args.no_prune else f"; {stats['missing']:,} now missing"
        # errors are the scan's error records, which include unreadable folders.
        print(f"[bytecat] merged {stats['files']:,} files, {stats['volumes']} volume(s)"
              f"{missing}; {stats['errors']:,} paths unreadable")
    if stats["bad"]:
        print(f"[bytecat] skipped {stats['bad']:,} malformed lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
