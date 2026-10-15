#!/usr/bin/env python3
"""Duplicate report. Reads the catalog, writes a Markdown file. Deletes NOTHING.

This tool has no delete path, no move path, and no --apply flag. That is
deliberate: the decision about which copy is the keeper is yours, and a
reporting tool that could also delete is one flag away from being a disaster.

The report is written to a file rather than stdout so that its contents -- real
paths from a personal archive -- stay on disk and out of any agent transcript.
The terminal gets aggregate counts only.

Usage:
    report_dupes.py                      # -> ~/.local/share/bytecat/reports/
    report_dupes.py --min-bytes 10485760 # only groups wasting >= 10 MB
    report_dupes.py --cross-host-only
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import db


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def collect(conn, *, min_bytes: int, cross_host_only: bool) -> list[dict]:
    """Group present files by blob, keeping only genuine multi-copy groups."""
    rows = conn.execute("""
        SELECT b.id AS blob_id, b.hash, b.size_bytes,
               f.id AS file_id, f.relpath, f.inode, f.nlink,
               v.uuid AS vol_uuid, v.label AS vol_label, v.last_mount,
               v.state AS vol_state, h.name AS host
        FROM blobs b
        JOIN files f  ON f.blob_id = b.id AND f.state = 'present'
        JOIN volumes v ON v.id = f.volume_id
        JOIN hosts   h ON h.id = v.host_id
        WHERE b.n_files > 1
        ORDER BY b.size_bytes DESC, b.id
    """).fetchall()

    groups: dict[int, list] = defaultdict(list)
    for r in rows:
        groups[r["blob_id"]].append(r)

    out = []
    for blob_id, members in groups.items():
        # Hardlinks are one physical file with several names. Counting them as
        # duplicates would invent wasted space that does not exist.
        physical: dict[tuple, list] = defaultdict(list)
        for m in members:
            key = (m["host"], m["vol_uuid"], m["inode"]) if m["nlink"] and m["nlink"] > 1 \
                else (m["host"], m["vol_uuid"], m["file_id"])
            physical[key].append(m)

        n_physical = len(physical)
        if n_physical < 2:
            continue

        size = members[0]["size_bytes"]
        wasted = size * (n_physical - 1)   # keeping one copy is not waste
        if wasted < min_bytes:
            continue

        hosts = {m["host"] for m in members}
        volumes = {(m["host"], m["vol_uuid"]) for m in members}
        if cross_host_only and len(hosts) < 2:
            continue

        out.append({
            "blob_id": blob_id,
            "hash": members[0]["hash"],
            "size": size,
            "n_copies": n_physical,
            "n_paths": len(members),
            "wasted": wasted,
            "hosts": sorted(hosts),
            "n_volumes": len(volumes),
            "members": members,
        })

    out.sort(key=lambda g: g["wasted"], reverse=True)
    return out


def write_report(groups: list[dict], path: Path, *, top: int,
                 cross_host_only: bool = False, filters: str = "",
                 n_unhashed: int = 0) -> dict:
    total_wasted = sum(g["wasted"] for g in groups)
    cross_host = [g for g in groups if len(g["hosts"]) > 1]
    one_host = [g for g in groups if len(g["hosts"]) == 1]
    cross_host_wasted = sum(g["wasted"] for g in cross_host)

    lines: list[str] = []
    add = lines.append

    add(f"# Duplicate report, {datetime.now():%Y-%m-%d %H:%M}")
    add("")
    add("## Summary")
    add("")
    add(f"- **{len(groups):,}** groups of identical files")
    add(f"- **{human(total_wasted)}** reclaimable if you kept one copy of each")
    if not cross_host_only:
        # Under --cross-host-only every group spans machines: the line would
        # repeat the one above.
        add(f"- **{len(cross_host):,}** groups span more than one machine "
            f"({human(cross_host_wasted)})")
    add("")
    # Without these two lines a filtered or partly hashed run reads as the
    # whole catalog.
    if filters:
        add(f"Filtered: {filters}")
        add("")
    if n_unhashed:
        add(f"{n_unhashed:,} files not hashed yet")
        add("")
    if not groups:
        add("No duplicates found.")
        add("")

    def members(g: dict) -> None:
        # Names that share host, volume and inode are one physical file (the
        # key collect() uses), so deleting one of them frees nothing.
        names: dict[tuple, int] = defaultdict(int)
        for m in g["members"]:
            if m["nlink"] and m["nlink"] > 1:
                names[(m["host"], m["vol_uuid"], m["inode"])] += 1
        for m in g["members"]:
            flag = "" if m["vol_state"] == "online" else "  _(volume offline)_"
            if m["nlink"] and m["nlink"] > 1 and \
                    names[(m["host"], m["vol_uuid"], m["inode"])] > 1:
                flag += "  _(hardlink)_"
            add(f"- `{m['host']}` [{m['vol_label'] or m['vol_uuid'][:8]}] "
                f"`{m['relpath']}`{flag}")

    def more(section: list[dict]) -> None:
        # One line per truncated section; --top of all groups lists every section.
        if len(section) > top:
            add(f"_…and {len(section) - top:,} more groups; "
                f"`--top {len(groups)}` lists all._")
            add("")

    if cross_host:
        add("## Same content on more than one machine")
        add("")
        for g in cross_host[:top]:
            add(f"### {human(g['size'])} × {g['n_copies']} copies "
                f"({human(g['wasted'])} reclaimable)")
            add("")
            add(f"`{g['hash']}` · hosts: {', '.join(g['hosts'])}")
            add("")
            members(g)
            add("")
        more(cross_host)

    # Each group is listed once: this section holds only single-machine groups,
    # and goes without a heading when it is the only section.
    if one_host:
        if cross_host:
            add("## Same content on one machine")
            add("")
        for g in one_host[:top]:
            add(f"### {human(g['size'])} × {g['n_copies']} copies "
                f"({human(g['wasted'])} reclaimable)")
            add("")
            add(f"`{g['hash']}`")
            add("")
            members(g)
            add("")
        more(one_host)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))

    return {
        "groups": len(groups),
        "wasted": total_wasted,
        "cross_host": len(cross_host),
        "cross_host_wasted": cross_host_wasted,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="bytecat duplicate report (read-only)")
    ap.add_argument("--min-bytes", type=int, default=1,
                    help="ignore groups reclaiming less than this")
    ap.add_argument("--cross-host-only", action="store_true")
    ap.add_argument("--top", type=int, default=500, help="groups to list")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    try:
        conn = db.connect(readonly=True)
    except db.NoCatalog as e:
        # Run as a child process by `bytecat ask` / `bytecat dupes`, so this never
        # reaches __main__.py. An empty catalog is a thing to be told.
        print(f"[bytecat] {e}", file=sys.stderr)
        return 2
    groups = collect(conn, min_bytes=args.min_bytes,
                     cross_host_only=args.cross_host_only)
    n_unhashed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE state = 'present' AND t1_hashed = 0"
    ).fetchone()[0]
    filters = ", ".join(
        ([f"--min-bytes {args.min_bytes}"] if args.min_bytes > 1 else [])
        + (["--cross-host-only"] if args.cross_host_only else []))

    out = Path(args.out) if args.out else (
        db.atlas_home() / "reports" / f"duplicates-{datetime.now():%Y%m%d-%H%M}.md"
    )
    s = write_report(groups, out, top=args.top, cross_host_only=args.cross_host_only,
                     filters=filters, n_unhashed=n_unhashed)

    # Terminal output is aggregate only -- the paths live in the file.
    line = (f"[bytecat] {s['groups']:,} duplicate groups · "
            f"{human(s['wasted'])} reclaimable")
    if not args.cross_host_only:
        # Under --cross-host-only this clause would repeat the group count.
        line += (f" · {s['cross_host']:,} span multiple machines "
                 f"({human(s['cross_host_wasted'])})")
    print(line)
    print(f"[bytecat] report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
