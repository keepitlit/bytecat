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
    report_coverage.py --source-volume <uuid> --target-host <name> [...]
    report_coverage.py --source-volume <uuid> --target-volume <uuid> [...]
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


def resolve_volumes(conn, uuids: list[str], hosts: list[str]) -> list[int]:
    ids: list[int] = []
    for u in uuids:
        r = conn.execute("SELECT id FROM volumes WHERE uuid = ?", (u,)).fetchall()
        if not r:
            sys.exit(f"[bytecat] no volume with uuid {u!r}")
        ids += [x["id"] for x in r]
    for h in hosts:
        r = conn.execute(
            "SELECT v.id FROM volumes v JOIN hosts h ON h.id = v.host_id "
            "WHERE h.name = ?", (h,)).fetchall()
        if not r:
            sys.exit(f"[bytecat] no volumes for host {h!r}")
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


def write_report(res: dict, out: Path, src_label: str, tgt_label: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    b = res["buckets"]
    with out.open("w") as fh:
        fh.write(f"# Coverage report\n\nSource: {src_label}\nTargets: {tgt_label}\n")
        fh.write(f"Generated: {datetime.now():%Y-%m-%d %H:%M}\n\n")
        fh.write("Content-hash set membership. Nothing was deleted or moved.\n\n")
        for name, title in (
            ("present", "Confirmed present on a target (by content)"),
            ("missing", "NOT on any target -- fully hashed, content confirmed absent"),
            ("unique",  "NOT on any target -- proved by unique size, never read"),
            ("unhashed", "No claim yet -- tier 1 has not reached these"),
        ):
            folders = b.get(name, {})
            n = sum(v[0] for v in folders.values())
            sz = sum(v[1] for v in folders.values())
            fh.write(f"## {title}\n\n{n:,} files, {human(sz)}\n\n")
            if folders:
                fh.write("| Folder | Files | Bytes |\n|---|---:|---:|\n")
                for k, v in sorted(folders.items(), key=lambda kv: -kv[1][1]):
                    fh.write(f"| {k} | {v[0]:,} | {human(v[1])} |\n")
                fh.write("\n")
        fh.write("## Every file not found on a target\n\n")
        fh.write("| Path | Bytes | Why |\n|---|---:|---|\n")
        for p, s, why in sorted(res["missing_paths"], key=lambda x: -x[1]):
            fh.write(f"| {p} | {s:,} | {why} |\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="bytecat coverage report (read-only)")
    ap.add_argument("--source-volume", required=True)
    ap.add_argument("--target-volume", action="append", default=[])
    ap.add_argument("--target-host", action="append", default=[])
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    conn = db.open_catalog()
    src = resolve_volumes(conn, [args.source_volume], [])
    if len(src) != 1:
        sys.exit("[bytecat] --source-volume must name exactly one volume")
    targets = [v for v in resolve_volumes(conn, args.target_volume, args.target_host)
               if v != src[0]]
    if not targets:
        sys.exit("[bytecat] no target volumes (and a volume cannot be its own target)")

    res = classify(conn, src[0], targets)
    out = Path(args.out) if args.out else (
        db.atlas_home() / "reports" / f"coverage-{datetime.now():%Y%m%d-%H%M}.md")
    write_report(res, out, args.source_volume, ", ".join(map(str, targets)))

    b = res["buckets"]
    for name in ("present", "missing", "unique", "unhashed"):
        f = b.get(name, {})
        print(f"[bytecat] {name:9s} {sum(v[0] for v in f.values()):>9,} files  "
              f"{human(sum(v[1] for v in f.values())):>10s}")
    print(f"[bytecat] report written to {out}")
    print("[bytecat] nothing was deleted or moved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
