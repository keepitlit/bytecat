#!/usr/bin/env python3
"""Remove a drive, or a whole host, from the catalog.

For a drive that was wiped, sold or scanned by mistake. Only catalog rows go:
no file on disk is opened, moved or deleted. Search vectors stay in Chroma
until `embed --reset`; ask ignores one whose file is gone (ask.current).
"""

from __future__ import annotations

import argparse
import sys

from . import db
from .hash import refresh_fanout


def chosen(conn, volume: str, host: str) -> list:
    """The drives --volume and --host name; exits when they name none."""
    q = ("SELECT v.id, v.uuid, v.label, v.last_mount, h.name AS host, "
         "(SELECT COUNT(*) FROM files f WHERE f.volume_id = v.id) n "
         "FROM volumes v JOIN hosts h ON h.id = v.host_id")
    rows = conn.execute(q + " ORDER BY h.name, v.id").fetchall()
    if host and not conn.execute("SELECT 1 FROM hosts WHERE name = ?", (host,)).fetchone():
        sys.exit(f"[bytecat] no host {host!r}; `bytecat status` lists them")
    if volume:
        ids = set(db.resolve_volume(conn, volume))
        rows = [r for r in rows if r["id"] in ids]
    if host:
        rows = [r for r in rows if r["host"] == host]
    if volume and not rows:
        sys.exit(f"[bytecat] no volume {volume!r} on {host}")
    return rows


def forget(conn, volume_ids: list[int], host: str = "") -> int:
    """Delete the drives' rows and whatever only they used. Returns files removed."""
    # A host with no drives left still has its own row to remove.
    ph = ",".join("?" * len(volume_ids)) or "NULL"
    with db.transaction(conn):
        file_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM files WHERE volume_id IN ({ph})", volume_ids)]
        for fid in file_ids:
            conn.execute("DELETE FROM files_fts WHERE rowid = ?", (fid,))
        # extractions and errors follow by ON DELETE CASCADE.
        conn.execute(f"DELETE FROM files WHERE volume_id IN ({ph})", volume_ids)
        conn.execute(f"DELETE FROM volumes WHERE id IN ({ph})", volume_ids)
        if host:
            conn.execute("DELETE FROM hosts WHERE name = ? AND id NOT IN "
                         "(SELECT host_id FROM volumes)", (host,))
        conn.execute("DELETE FROM blobs WHERE id NOT IN "
                     "(SELECT blob_id FROM files WHERE blob_id IS NOT NULL)")
        refresh_fanout(conn)
    return len(file_ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bytecat forget",
        description="remove a drive or host from the catalog; files on disk stay")
    ap.add_argument("--volume", default="", metavar="DRIVE",
                    help="drive to remove: UUID, label or mount path")
    ap.add_argument("--host", default="",
                    help="host to remove, or the host --volume is on")
    ap.add_argument("--yes", action="store_true", help="do not ask first")
    args = ap.parse_args(argv)
    if not (args.volume or args.host):
        ap.error("pass --volume or --host")

    if not db.db_path().exists():
        print(f"[bytecat] no catalog at {db.db_path()}", file=sys.stderr)
        return 2
    conn = db.open_catalog()
    rows = chosen(conn, args.volume, args.host)
    whole_host = args.host if not args.volume else ""
    lines = [f"  {r['host']}  {r['last_mount'] or r['label'] or '-'}  {r['uuid']}  "
             f"{r['n']:,} files" for r in rows]

    if not args.yes:
        print("\n".join(lines + [f"  host {whole_host}"] if whole_host else lines))
        try:
            answer = input("forget these [y/N]? ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("[bytecat] nothing forgotten", file=sys.stderr)
            return 1

    n = forget(conn, [r["id"] for r in rows], whole_host)
    print("\n".join(lines))
    print(f"[bytecat] forgot {len(rows)} drive(s), {n:,} files"
          + (f", host {whole_host}" if whole_host else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
