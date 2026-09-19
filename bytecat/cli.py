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
        print(f"catalog: {db.db_path()}")
        print(f"  {e}".replace("\n", "\n  "))
        return 0

    hosts = conn.execute("""
        SELECT h.name, COUNT(DISTINCT v.id) AS vols,
               SUM(CASE WHEN v.state='online' THEN 1 ELSE 0 END) AS online
        FROM hosts h LEFT JOIN volumes v ON v.host_id = h.id
        GROUP BY h.id ORDER BY h.name
    """).fetchall()

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

    print(f"catalog: {db.db_path()}")
    print(f"files:   {present:,} present  ({gb(tot['bytes'])}), "
          f"{tot['missing'] or 0:,} missing")
    print()
    print("hosts:")
    for h in hosts:
        print(f"  {h['name']:<20} {h['vols']} volume(s), {h['online'] or 0} online")
    print()
    print("tiers (of present files):")
    for label, key in (("t1 hashed", "t1"), ("t2 metadata", "t2"),
                       ("t3 embedded", "t3"), ("t4 enriched", "t4")):
        done = tiers[key] or 0
        pct = (100 * done / present) if present else 0
        print(f"  {label:<14} {done:>10,} / {present:,}  ({pct:.1f}%)")
    print()
    print(f"duplicates: {dupes['groups']:,} groups, {gb(dupes['wasted'])} reclaimable")

    recent = conn.execute("""
        SELECT stage, status, started_at, finished_at, n_seen, n_errors
        FROM runs ORDER BY id DESC LIMIT 5
    """).fetchall()
    if recent:
        print("\nrecent runs:")
        for r in recent:
            print(f"  {r['started_at']}  {r['stage']:<8} {r['status']:<6} "
                  f"seen={r['n_seen']:,} err={r['n_errors']}")
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
            print(f"[bytecat] {name} failed; stopping. "
                  f"Progress is saved -- fix and re-run.", file=sys.stderr)
            return r.returncode
    print("\n[bytecat] pipeline complete. `python3 -m bytecat status` for a summary.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bytecat",
                                 description="local-only catalog of your files")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="where everything stands")

    p = sub.add_parser("pipeline", help="run hash + extract + embed in order")
    p.add_argument("--limit", type=int, default=0)

    for name, mod, helptext in (
        ("scan", "bytecat.scan", "walk a tree, emit NDJSON"),
        ("merge", "bytecat.merge", "ingest scanner NDJSON"),
        ("hash", "bytecat.hash", "tier 1 content hashing"),
        ("extract", "bytecat.extract", "tier 2 metadata"),
        ("embed", "bytecat.embed", "tier 3 text + vectors"),
        ("enrich", "bytecat.enrich", "tier 4 LLM labels (budgeted)"),
        ("ask", "bytecat.ask", "search the catalog"),
        ("eval", "bytecat.eval", "measure retrieval quality"),
        ("dupes", "bytecat.report_dupes", "write the duplicate report"),
        ("coverage", "bytecat.report_coverage", "what is NOT stored anywhere else"),
        ("audit", "bytecat.audit", "re-read the disk and cross-check a catalog verdict"),
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
