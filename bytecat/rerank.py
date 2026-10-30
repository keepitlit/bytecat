#!/usr/bin/env python3
"""Reorder search results with the local chat model.

Fusion gets the right files into the pool and then leaves them in the wrong
order. Measured on this catalog: recall@pool 0.973 against MRR 0.775 -- the
answer is nearly always retrieved and nearly never on top. Query expansion is
the wrong instrument for that gap, because there is nothing left to find; the
gap is judgement about which of the retrieved files actually answers the
question, which is what a language model is for.

This also supplies the relevance floor the pipeline otherwise cannot express.
rrf() fuses on rank alone and discards the vector distance, so no absolute score
survives to threshold against -- but a per-candidate verdict does the same job
without re-plumbing the fusion.

Only metadata is sent: path, kind, size, title, gist. No file content, and it
goes to the model server on loopback like everything else (see llm.py).

Degrades rather than fails: if the model is unreachable or answers with
something unusable, the caller keeps the fused order.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .llm import LMStudio, failure_reason, note_once

SYSTEM = (
    "You rank file-search results. You are given a user's query and a numbered "
    "list of candidate files, each with its path and a short description. "
    "For every candidate, judge whether it actually answers the query.\n"
    "Reply with JSON only: {\"results\": [{\"id\": <int>, \"relevant\": <bool>, "
    "\"score\": <0-3>}]}\n"
    "score 3 = directly answers it, 2 = clearly related, 1 = weak or partial, "
    "0 = unrelated. Set relevant to false for anything scoring 0.\n"
    "Include every candidate id exactly once. Judge only from what you are "
    "shown. Boilerplate, caches, templates and empty files are not answers to "
    "a question about their subject matter."
)

RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "score": {"type": "integer"},
                },
                "required": ["id", "relevant", "score"],
            },
        },
    },
    "required": ["results"],
}

GIST_CHARS = 110      # enough to judge by, short enough to fit a page of them
# A ceiling, not a target. hydrate() collapses duplicate blobs before this
# runs, so a 30-deep fused pool routinely arrives here as ~10 distinct
# files. Raising it does not cost what it looks like it costs.
MAX_CANDIDATES = 30
MAX_TOKENS = 3000     # a reasoning model narrates before it answers


def _size(n: int) -> str:
    x = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(x) < 1024 or unit == "GB":
            return f"{int(x)}{unit}" if unit == "B" else f"{x:.0f}{unit}"
        x /= 1024
    return f"{x:.0f}GB"


def candidate_block(results: list[dict]) -> str:
    lines = []
    for r in results:
        desc = r.get("title") or ""
        gist = (r.get("gist") or "")[:GIST_CHARS]
        if gist:
            desc = f"{desc} -- {gist}" if desc else gist
        lines.append(
            f"id={r['id']} | {r['relpath']} | {r.get('kind') or '?'} | "
            f"{_size(r.get('size_bytes'))} | {desc or '(no description)'}"
        )
    return "\n".join(lines)


def parse_verdicts(raw: str, valid: set[int]) -> dict[int, tuple[bool, int]]:
    """Pull id -> (relevant, score) out of the reply, discarding anything odd.

    Every id is checked against the pool. A reasoning model will occasionally
    invent one, and an invented id that survived would promote a file the search
    never actually retrieved.
    """
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}

    out: dict[int, tuple[bool, int]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            fid = int(it["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if fid not in valid or fid in out:
            continue
        score = it.get("score", 0)
        score = int(score) if isinstance(score, (int, float)) else 0
        out[fid] = (bool(it.get("relevant", score > 0)), max(0, min(3, score)))
    return out


def rerank(query: str, results: list[dict], *,
           lm: Optional[LMStudio] = None) -> list[dict]:
    """Return `results` reordered by judged relevance. Never drops a file.

    Judged-irrelevant candidates are demoted below every relevant one rather
    than deleted. A caller slicing to --limit stops seeing them, which is the
    behaviour we want, but a wider limit can still reach them -- deleting on a
    small model's say-so would make the catalog quietly lossy.
    """
    if len(results) < 2:
        return results
    pool = results[:MAX_CANDIDATES]
    tail = results[MAX_CANDIDATES:]
    valid = {r["id"] for r in pool}

    try:
        lm = lm or LMStudio()
        raw = lm.chat(SYSTEM, f"query: {query}\n\ncandidates:\n{candidate_block(pool)}",
                      max_tokens=MAX_TOKENS, schema=RERANK_SCHEMA)
    except Exception as e:
        # Results keep the fused order.
        note_once(f"[bytecat] --smart skipped: {failure_reason(e)}")
        return results

    verdicts = parse_verdicts(raw, valid)
    if not verdicts:
        note_once("[bytecat] --smart skipped: unusable model reply")
        return results

    # Fused rank is the tiebreak, so files the model scored equally keep the
    # order the retrievers agreed on instead of being shuffled arbitrarily.
    fused_rank = {r["id"]: i for i, r in enumerate(pool)}

    def key(r: dict):
        relevant, score = verdicts.get(r["id"], (True, 1))
        return (0 if relevant else 1, -score, fused_rank[r["id"]])

    ordered = sorted(pool, key=key)
    for r in ordered:
        relevant, score = verdicts.get(r["id"], (True, 1))
        r["rerank_score"] = score
        r["rerank_relevant"] = relevant
    return ordered + tail
