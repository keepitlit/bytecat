#!/usr/bin/env python3
"""Single entry point. `python3 -m bytecat <command>`.

Subcommands mirror the tiers, plus `pipeline` to run the cheap ones in order and
`status` to see where a long job got to.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import db


def cmd_status(args) -> int:
    try:
        conn = db.connect(readonly=True)
    except db.NoCatalog as e:
        # A fresh install is not an error condition; report it and exit clean.
        # The message already names the catalog path, so it is printed once.
        print(e)
        return 0

    # No online/offline figure: nothing ever marks a volume offline
    # (db.mark_volume_offline has no caller), so it would always equal the count.
    vols = conn.execute("""
        SELECT h.name host, v.uuid, v.label, v.last_mount, v.last_scan,
               (SELECT COUNT(*) FROM files f
                 WHERE f.volume_id = v.id AND f.state = 'present') n
        FROM volumes v JOIN hosts h ON h.id = v.host_id
        ORDER BY h.name, v.last_mount
    """).fetchall()

    if not vols:
        # A catalog file with nothing merged into it yet (pipeline on a fresh
        # install creates one): the zero rows would read like a result.
        print(f"nothing catalogued in {db.db_path()} yet. "
              "Start with: bytecat scan ~/Documents | bytecat merge")
        return 0

    tot = conn.execute("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN state='present' THEN 1 ELSE 0 END) present,
               SUM(CASE WHEN state='missing' THEN 1 ELSE 0 END) missing,
               COALESCE(SUM(CASE WHEN state='present' THEN size_bytes ELSE 0 END),0) bytes
        FROM files
    """).fetchone()

    tiers = conn.execute("""
        SELECT SUM(t1_hashed) t1, SUM(t2_meta) t2,
               SUM(t3_embedded) t3, SUM(t4_enriched) t4
        FROM files WHERE state='present'
    """).fetchone()

    dupes = conn.execute("""
        SELECT COUNT(*) groups,
               COALESCE(SUM((n_files-1)*size_bytes),0) wasted
        FROM blobs WHERE n_files > 1
    """).fetchone()

    def gb(n): return f"{(n or 0)/1024**3:.1f} GB"
    present = tot["present"] or 0

    print(f"files:   {present:,} present  ({gb(tot['bytes'])}), "
          f"{tot['missing'] or 0:,} missing")
    print()
    # One line per drive, led by its mount: scans on Linux or with
    # --volume-uuid store no label, so a label column would be mostly blank.
    # No cap: a "+N more" line would hide the drive being looked for.
    host = None
    for v in vols:
        if v["host"] != host:
            host = v["host"]
            print(host)
        label = f"  {v['label']}" if v["label"] else ""
        scanned = (v["last_scan"] or "")[:10]
        print(f"  {v['last_mount'] or '-'}{label}  {v['uuid']}  "
              f"{v['n']:,} files  scanned {scanned}")
    print()
    print("progress:")
    # Each row is named after the command that advances it.
    for label, key in (("hash", "t1"), ("extract", "t2"),
                       ("embed", "t3"), ("enrich", "t4")):
        done = tiers[key] or 0
        pct = (100 * done / present) if present else 0
        print(f"  {label:<10} {done:>10,} / {present:,}  ({pct:.1f}%)")
    print()
    print(f"duplicates: {dupes['groups']:,} groups, {gb(dupes['wasted'])} reclaimable")

    recent = conn.execute("""
        SELECT stage, status, started_at, finished_at, n_seen, n_errors
        FROM runs ORDER BY id DESC LIMIT 5
    """).fetchall()
    if recent:
        print("\nrecent runs:")
        for r in recent:
            # started_at is SQLite datetime('now'), which is UTC.
            errs = f", {r['n_errors']} error(s)" if r['n_errors'] else ""
            print(f"  {r['started_at']} UTC  {r['stage']:<8} {r['status']:<6}  "
                  f"{r['n_seen']:,} seen{errs}")
    return 0


def cmd_pipeline(args) -> int:
    """Run the cheap tiers in order. Tier 4 is excluded on purpose -- it costs
    real time and should always be an explicit, budgeted decision."""
    steps = [
        ("hash", [sys.executable, "-m", "bytecat.hash"]),
        ("extract", [sys.executable, "-m", "bytecat.extract"]),
        ("embed", [sys.executable, "-m", "bytecat.embed"]),
    ]
    if args.limit:
        steps[1][1].extend(["--limit", str(args.limit)])
        steps[2][1].extend(["--limit", str(args.limit)])

    for name, cmd in steps:
        print(f"\n=== {name} ===", flush=True)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            # Each step commits per batch, so a re-run resumes where it stopped.
            print(f"[bytecat] {name} failed, pipeline stopped; re-run to resume.",
                  file=sys.stderr)
            return r.returncode
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bytecat",
                                 description="local-only catalog of your files")
    # metavar: errors say "required: command" and the verb list prints once.
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command",
                            title="commands")

    sub.add_parser("status", help="summarise the catalog")

    p = sub.add_parser("pipeline", help="run hash + extract + embed in order")
    p.add_argument("--limit", type=int, default=0)

    for name, mod, helptext in (
        ("scan", "bytecat.scan", "list files under paths; pipe into merge"),
        ("merge", "bytecat.merge", "add scan output to the catalog"),
        ("hash", "bytecat.hash", "hash file contents"),
        ("extract", "bytecat.extract", "read file metadata"),
        ("embed", "bytecat.embed", "index files for search"),
        ("enrich", "bytecat.enrich", "label files with a local LLM, up to --budget"),
        ("ask", "bytecat.ask", "search the catalog"),
        ("eval", "bytecat.eval", "measure search quality"),
        ("dupes", "bytecat.report_dupes", "write the duplicate report"),
        ("coverage", "bytecat.report_coverage",
         "report a volume's files missing from the targets"),
        ("audit", "bytecat.audit", "re-check coverage or dupes against the disk"),
    ):
        sp = sub.add_parser(name, help=helptext, add_help=False)
        sp.set_defaults(_module=mod)

    # parse_known_args, not REMAINDER: REMAINDER stops collecting the moment the
    # first token looks like an option, so `bytecat enrich --budget 10` died as an
    # unrecognized argument while `bytecat scan <path>` worked. Everything we do
    # not recognise belongs to the subcommand's own parser.
    args, rest = ap.parse_known_args(argv)
    args.rest = rest

    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "pipeline":
        return cmd_pipeline(args)

    return subprocess.run(
        [sys.executable, "-m", args._module, *args.rest]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
