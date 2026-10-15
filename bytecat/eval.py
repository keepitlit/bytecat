#!/usr/bin/env python3
"""Measure retrieval quality against a fixed query set.

Every ranking change should be justified by a number, not by how good a handful
of spot-checked queries look. This corpus has already produced one confident
hypothesis that measurement refuted -- adding nomic's search_query:/search_document:
prefixes, which made the ranking worse -- so the ruler comes before the changes.

Metrics, all computed over one search per query:

  MRR          mean reciprocal rank of the first correct answer. Rewards putting
               a right answer at the very top, not merely somewhere on the page.
  recall@5     share of a query's correct answers that made the top 5.
  hit@5        share of queries with at least one correct answer in the top 5.
  recall@pool  share found anywhere in the retrieved pool. This separates the two
               failure modes: a low value means retrieval never surfaced the file
               and no amount of reranking can save it; a high value with a low MRR
               means the answer is present and merely badly ordered.
  attractor    mean count of known-useless files occupying top-5 slots.

Usage:
    eval.py                          # run and print the scoreboard
    eval.py --queries my.json        # a query set of your own (see tests/eval_queries.json)
    eval.py --json runs/base.json    # also save machine-readable results
    eval.py --baseline runs/base.json  # print deltas against a saved run

The bundled query set targets the fictional corpus in tests/fixtures/eval-corpus;
index that into a scratch catalog (BYTECAT_HOME=$(mktemp -d)) to run it. Write
your own set against your own catalog to measure ranking changes on real data.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import ask, db

QUERIES_PATH = Path(__file__).resolve().parent.parent / "tests" / "eval_queries.json"
POOL = 30
TOP = 5


def is_noise(relpath: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(relpath, g) for g in globs)


def score_one(results: list[dict], expect: list[str], noise: list[str]) -> dict:
    paths = [r["relpath"] for r in results]
    want = set(expect)

    rank = next((i + 1 for i, p in enumerate(paths) if p in want), 0)
    top = paths[:TOP]
    found_top = want & set(top)

    return {
        "rr": 1.0 / rank if rank else 0.0,
        "first_rank": rank,
        "recall_top": len(found_top) / len(want) if want else 0.0,
        "hit_top": 1.0 if found_top else 0.0,
        "recall_pool": len(want & set(paths)) / len(want) if want else 0.0,
        "attractors": sum(1 for p in top if is_noise(p, noise)),
        "top": top,
    }


def run(conn, spec: dict, *, smart: bool = False) -> dict:
    noise = spec.get("noise_globs", [])
    per = []
    for item in spec["queries"]:
        kwargs = {"limit": POOL, "pool": POOL}
        if smart:
            kwargs["smart"] = True
        results = ask.search(conn, item["q"], **kwargs)
        s = score_one(results, item["expect"], noise)
        s["q"] = item["q"]
        s["tier"] = item.get("tier", "")
        per.append(s)
    return {"pool": POOL, "top": TOP, "smart": smart, "per_query": per,
            "aggregate": aggregate(per)}


def aggregate(per: list[dict]) -> dict:
    def mean(key, rows):
        return sum(r[key] for r in rows) / len(rows) if rows else 0.0

    out = {}
    for label, rows in (("all", per),
                        ("direct", [r for r in per if r["tier"] == "direct"]),
                        ("abstract", [r for r in per if r["tier"] == "abstract"])):
        out[label] = {
            "n": len(rows),
            "mrr": mean("rr", rows),
            "recall_top": mean("recall_top", rows),
            "hit_top": mean("hit_top", rows),
            "recall_pool": mean("recall_pool", rows),
            "attractors": mean("attractors", rows),
        }
    return out


def render(res: dict, baseline: Optional[dict] = None) -> str:
    out = []
    out.append(f"{'query':<42} {'rank':>5} {'r@5':>6} {'attr':>5}")
    out.append("-" * 62)
    for r in res["per_query"]:
        rank = str(r["first_rank"]) if r["first_rank"] else "--"
        flag = "  <-- miss" if not r["hit_top"] else ""
        out.append(f"{r['q'][:42]:<42} {rank:>5} {r['recall_top']:>6.2f} "
                   f"{r['attractors']:>5}{flag}")

    out.append("")
    head = f"{'set':<10} {'n':>3} {'MRR':>7} {'recall@5':>9} {'hit@5':>7} " \
           f"{'recall@pool':>12} {'attractors':>11}"
    out.append(head)
    out.append("-" * len(head))
    for label in ("all", "direct", "abstract"):
        a = res["aggregate"][label]
        line = (f"{label:<10} {a['n']:>3} {a['mrr']:>7.3f} {a['recall_top']:>9.3f} "
                f"{a['hit_top']:>7.3f} {a['recall_pool']:>12.3f} "
                f"{a['attractors']:>11.2f}")
        if baseline:
            b = baseline["aggregate"][label]
            d = a["mrr"] - b["mrr"]
            line += f"   MRR {d:+.3f}"
        out.append(line)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="measure bytecat retrieval quality")
    ap.add_argument("--json", default="", help="write results to this path")
    ap.add_argument("--baseline", default="",
                    help="MRR change vs a saved --json run")
    ap.add_argument("--smart", action="store_true",
                    help="score with ask --smart reranking")
    ap.add_argument("--queries", default=str(QUERIES_PATH),
                    help="query set (default: tests/eval_queries.json)")
    args = ap.parse_args()

    spec = json.loads(Path(args.queries).expanduser().read_text())
    try:
        conn = db.connect(readonly=True)
    except db.NoCatalog as e:
        print(f"[bytecat] {e}", file=sys.stderr)
        return 2

    res = run(conn, spec, smart=args.smart)

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text())
        except OSError as e:
            print(f"[bytecat] cannot read baseline: {e}", file=sys.stderr)

    print(render(res, baseline))

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res, indent=2))
        print(f"\n[bytecat] wrote {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
