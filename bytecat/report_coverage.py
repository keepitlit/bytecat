#!/usr/bin/env python3
"""Is every file on one volume also stored, by content, somewhere else?

The backup-verification counterpart to report_dupes.py. That one asks "what is
redundant"; this asks "what is NOT redundant" -- the question you need answered
before retiring a drive.

Set membership by content hash, never by path: a file that was re-filed under a
different name on the target still counts as present. That is the whole point;
a path-by-path diff would report thousands of false gaps.

Like report_dupes.py this DELETES NOTHING and has no --apply. Per-file paths go
to a file under ~/.local/share/bytecat/reports/, never to stdout -- the terminal
gets aggregates only, so a personal archive's paths stay out of any transcript.

Usage:
    report_coverage.py --source-volume <drive> --target-host <name> [...]
    report_coverage.py --source-volume <drive> --target-volume <drive> [...]

A <drive> is its UUID, its label or its mount, matched exactly.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import db


def human(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or u == "TB":
            return f"{x:.1f} {u}" if u != "B" else f"{int(x)} B"
        x /= 1024
    return f"{x:.1f} TB"


def top_level(relpath: str) -> str:
    head = relpath.split("/", 1)[0]
    return head if "/" in relpath else "(root)"


def resolve_volumes(conn, values: list[str], hosts: list[str]) -> list[int]:
    ids: list[int] = []
    for v in values:
        ids += db.resolve_volume(conn, v)
    for h in hosts:
        r = conn.execute(
            "SELECT v.id FROM volumes v JOIN hosts h ON h.id = v.host_id "
            "WHERE h.name = ?", (h,)).fetchall()
        if not r:
            names = [x["name"] for x in conn.execute(
                "SELECT name FROM hosts ORDER BY name")]
            known = f"; hosts: {', '.join(names)}" if names else ""
            sys.exit(f"[bytecat] no volumes for host {h!r}{known}")
        ids += [x["id"] for x in r]
    return sorted(set(ids))


def classify(conn, src_vol: int, targets: list[int]) -> dict:
    """Bucket every present file on the source volume.

    present  -- its blob has a present file row on a target volume
    missing  -- fully hashed, and no target holds that content
    unique   -- tier 1 proved its size unique, so it CANNOT be on a target.
                A real miss, but recorded separately: it was never read, so we
                hold no fingerprint of it.
    unhashed -- tier 1 has not reached it yet; no claim either way
    """
    ph = ",".join("?" * len(targets))
    rows = conn.execute(
        f"""SELECT f.relpath, f.size_bytes, f.blob_id, f.t1_hashed,
                   EXISTS (SELECT 1 FROM files g
                           WHERE g.blob_id = f.blob_id
                             AND g.volume_id IN ({ph})
                             AND g.state = 'present') AS on_target
              FROM files f
             WHERE f.volume_id = ? AND f.state = 'present'""",
        (*targets, src_vol),
    )

    buckets = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    missing_paths: list[tuple[str, int, str]] = []
    for r in rows:
        if r["t1_hashed"] == 0:
            b = "unhashed"
        elif r["blob_id"] is None:
            b = "unique"
        elif r["on_target"]:
            b = "present"
        else:
            b = "missing"
        agg = buckets[b][top_level(r["relpath"])]
        agg[0] += 1
        agg[1] += r["size_bytes"]
        if b in ("missing", "unique"):
            missing_paths.append((r["relpath"], r["size_bytes"], b))
    return {"buckets": buckets, "missing_paths": missing_paths}


def drive(conn, vol_id: int, *, with_uuid: bool) -> str:
    """Host and label (or mount) of a volume, as a person knows the drive."""
    v = conn.execute(
        "SELECT h.name AS host, v.uuid, v.label, v.last_mount FROM volumes v "
        "JOIN hosts h ON h.id = v.host_id WHERE v.id = ?", (vol_id,)).fetchone()
    name = v["label"] or v["last_mount"] or ""
    if with_uuid:
        return f"{v['host']} {name} ({v['uuid']})" if name else f"{v['host']} ({v['uuid']})"
    return f"{v['host']} {name or v['uuid']}"


def write_report(res: dict, out: Path, src_label: str, tgt_label: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    b = res["buckets"]

    def merged(*names):
        folders: dict = defaultdict(lambda: [0, 0])
        for n in names:
            for k, v in b.get(n, {}).items():
                folders[k][0] += v[0]
                folders[k][1] += v[1]
        return folders

    def section(fh, title, folders, always=False):
        n = sum(v[0] for v in folders.values())
        if not n and not always:
            return
        sz = sum(v[1] for v in folders.values())
        fh.write(f"## {title}\n\n{n:,} files, {human(sz)}\n\n")
        if folders:
            fh.write("| Folder | Files | Bytes |\n|---|---:|---:|\n")
            for k, v in sorted(folders.items(), key=lambda kv: -kv[1][1]):
                fh.write(f"| {k} | {v[0]:,} | {human(v[1])} |\n")
            fh.write("\n")

    with out.open("w") as fh:
        # Blank lines between header lines: Markdown joins consecutive lines.
        fh.write(f"# Coverage report, {datetime.now():%Y-%m-%d %H:%M}\n\n"
                 f"Source: {src_label}\n\nTargets: {tgt_label}\n\n")
        # Decision order: what to act on first. "Not on any target" always
        # shows, because its 0 is the verdict; the rest only when above 0.
        section(fh, "Not on any target", merged("missing", "unique"), always=True)
        section(fh, "Of which never read (unique size)", merged("unique"))
        if res["missing_paths"]:
            fh.write("## Every file not on a target\n\n")
            fh.write("| Path | Bytes | Why |\n|---|---:|---|\n")
            why_words = {"missing": "hashed", "unique": "by size"}
            for p, s, why in sorted(res["missing_paths"], key=lambda x: -x[1]):
                # A '|' in a name would split the row and shift the Why column.
                cell = p.replace("|", "\\|")
                fh.write(f"| {cell} | {s:,} | {why_words.get(why, why)} |\n")
            fh.write("\n")
        section(fh, "Not hashed yet", merged("unhashed"))
        section(fh, "On a target (by content)", merged("present"))


def main() -> int:
    ap = argparse.ArgumentParser(prog="bytecat coverage",
                                 description="bytecat coverage report (read-only)")
    ap.add_argument("--source-volume", required=True)
    ap.add_argument("--target-volume", action="append", default=[])
    ap.add_argument("--target-host", action="append", default=[])
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    try:
        conn = db.connect(readonly=True)
    except db.NoCatalog as e:
        print(f"[bytecat] {e}", file=sys.stderr)
        return 2
    src = resolve_volumes(conn, [args.source_volume], [])
    if len(src) != 1:
        sys.exit("[bytecat] --source-volume must name exactly one volume")
    targets = [v for v in resolve_volumes(conn, args.target_volume, args.target_host)
               if v != src[0]]
    if not targets:
        sys.exit("[bytecat] give --target-host or --target-volume (not the source)")

    res = classify(conn, src[0], targets)
    out = db.report_path("coverage", args.out)
    write_report(res, out, drive(conn, src[0], with_uuid=True),
                 ", ".join(drive(conn, t, with_uuid=True) for t in targets))

    # The answer first: what is not on a target, then what has no claim yet.
    b = res["buckets"]
    def total(*names):
        rows = [v for n in names for v in b.get(n, {}).values()]
        return sum(v[0] for v in rows), sum(v[1] for v in rows)
    lines = [("not on target", total("missing", "unique")),
             ("  of which never read", total("unique"))]
    if total("unhashed")[0]:
        lines.append(("not hashed yet", total("unhashed")))
    lines.append(("on target", total("present")))
    for label, (n, sz) in lines:
        print(f"[bytecat] {label:<21s} {n:>9,} files  {human(sz):>10s}")
    print(f"[bytecat] report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
