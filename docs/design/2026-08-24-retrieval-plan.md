> Dated design note (2026-08-24). Records the reasoning and measurements behind the
> ranking pipeline as it was built; commands and numbers reflect that date.

# Plan: fix retrieval, and put bonsai where it actually pays

Written 2026-08-24, after the first real tier-4 run on a personal notes folder (113 files,
107 enriched). Retrieval works well on direct queries and fails on abstract ones.
This is the plan to fix that, and to stop wasting the labels we already paid for.

## The diagnostic that shaped this

`ask "notes about money I am owed" --limit 30` on the enriched catalog:

    9. _TEMPLATE.md                        1.7KB   <- empty template
   11. cost.json                            206B
   12. 2026-06-11-vehicle-permit-reimburse.md 1.9KB  <- correct
   14. 2026-06-30-fuel-reimbursement.md     1.6KB  <- correct
   22. 2025-09-25-equipment-reimbursement   2.4KB  <- correct
   28-30. three identical 74-byte .json files, 45 copies each

**All three correct answers are in the pool. Recall@30 is 100%. The failure is
ordering, not retrieval.**

That single fact removes query expansion / HyDE from this plan entirely. Rewriting
the query helps you *find* documents you were missing; we are not missing any. The
whole job is ranking what we already have.

It also exposed a second, unrelated defect: ranks 28-30 are the *same* 74-byte blob
three times. `hydrate()` returns `n_copies` but never collapses by `blob_id`, so one
piece of duplicated content can occupy an unlimited number of result slots.

## Phase 0: build a ruler (no bonsai)

`python3 -m bytecat eval`: a fixed set of ~20 `query -> expected file(s)` pairs in
`tests/eval_queries.json`, reporting **recall@5**, **MRR**, and **attractor rate**
(share of top-5 slots held by files under ~300 bytes of real content).

This phase is first and is not optional. On this exact corpus I already formed a
confident hypothesis (that the missing nomic `search_query:` / `search_document:`
prefixes explained a bad ranking), and measurement refuted it: adding the prefixes
made the ranking *worse*. Every phase below is a guess until there is a number
attached to it.

Bias guard: queries are written by reading the corpus, never by reading rankings,
and the set includes controls that already pass ("taxes", "coffee brewing") so a
regression is visible rather than hidden behind an average.

Deliverable: a committed baseline scoreboard.

## Phase 1: spend what bonsai already produced (zero new inference)

**1a. Embed the gist.** `build_text()` (`bytecat/embed.py:29`) assembles path + name +
title + body. It never receives the gist, and `pending()` (`:60`) does not even
select it. So tier 4's one-sentence factual summary (the most query-shaped text in
the catalog) reaches FTS (`db.py:287` indexes it) but never reaches Chroma, where
it could match on meaning instead of on shared words. Add gist and category.

**1b. Collapse results by blob.** One entry per distinct blob, annotated with its
copy count. Structural, free, and it removes the 74-byte trio outright.

**1c.** `embed --reset`, re-run eval.

Expected: modest MRR gain, clear attractor-rate drop from 1b. **If adding the gist
makes the score worse, revert it**: model-written summaries share phrasing
("this file contains…") and could homogenize the vectors. That is exactly the shape
of the prefix lesson, so it gets measured rather than assumed.

Cost: no inference, ~2 minutes of re-embedding.

## Phase 2: close the tier-4 coverage hole

`html` sits in the `code` extension list (`db.py:34`), and `candidates()`
(`enrich.py:121`) accepts only `document,data,other`. So a saved web page or
an HTML export can never be labelled.

Fix by adding `code` to enrich's default kinds, **not** by reclassifying html as a
document. Reclassifying would break `ask --kind code`, and in a personal archive a
labelled shell script ("backs up the photo library to the NAS") is worth having
anyway.

Cost: 4 files here, ~35s. Scales with the drive.

## Phase 3: `ask --smart`, bonsai at query time

The main event, and the only place new inference is spent.

Retrieve a pool of 30 with the existing hybrid, hand bonsai one compact candidate
table (id, name, relpath, kind, size, title, gist, **no file content**) and get
back a schema-constrained verdict per candidate: `{id, relevant: bool, score: 0-3}`.
Drop the irrelevant, order by score, use the existing RRF rank as tiebreak.

Design notes that matter:

- **This is the relevance floor.** `rrf()` (`ask.py:88`) fuses on rank alone and
  throws the vector distance away, so there is no absolute score left to threshold
  against. A judgment call per candidate sidesteps that instead of re-plumbing it.
- **Hallucination guard**: validate every returned id against the pool and drop
  unknowns. A reasoning model will invent ids.
- **Must go through `llm.reply_text()`**: bonsai returns its answer in
  `reasoning_content`, not `content`.
- **Degrade like `vector_search()` does**: LM Studio down means a warning on stderr
  and the fused order, never an error.
- **One call for all 30 candidates**, not 30 calls. ~1.5k tokens in, a few hundred out.
- **Behind `--smart`, not default.** It turns a ~0.5s search into a ~3-6s one.
  Correct trade for an archive you search occasionally; wrong one to impose silently.

Ship gate: only if eval MRR improves materially over phase 1. If phase 1 alone
closes the gap, this does not get built.

## Phase 4 (deferred): the category vocabulary

67 of 107 files came back `technical`. Real, but the fix is not urgent, because
**category currently feeds nothing**: not the ranking, not FTS (`db.py:287` indexes
relpath/name/title/gist only), not the embedding. It is a dead field today.

Designing a taxonomy against 113 files of dev notes would also be fitting it to the
least representative corpus available. Revisit after a mixed personal corpus
(photos, receipts, scans, correspondence) has been through the pipeline.

## Security

Unchanged. Reranking sends metadata to LM Studio on loopback; enrichment already
sends full excerpts there. `llm._assert_local` and the non-loopback rejection stay
exactly as they are. Nothing new leaves the box.

## Order and stop conditions

    0  ruler
    1  gist + blob collapse   -> measure
    2  code kinds             -> measure
    3  rerank                 -> measure, ship only on a real gain

Stop after phase 1 if the numbers are already good enough. Phase 3 is the only part
that costs anything at query time, so it earns its place or it does not get merged.

---

# Results

## Phase 0: baseline (on the private notes folder; the baseline file is not shipped)

    set          n     MRR  recall@5   hit@5  recall@pool  attractors
    all         22   0.772     0.742   0.864        0.890        0.82
    direct       6   1.000     1.000   1.000        1.000        1.17
    abstract    16   0.686     0.646   0.812        0.849        0.69

The direct controls sit at a perfect MRR, which is the harness telling us it is
measuring the thing it claims to measure rather than being broken in a way that
flatters the next change.

## Phase 1: gist in the embedding, blob collapse in results

Decomposed, because the combined number hides which half did the work:

    baseline                MRR 0.772  recall@pool 0.890  attractors 0.82
    + gist                  MRR 0.769  recall@pool 0.973  attractors 0.50
    + gist + blob collapse  MRR 0.775  recall@pool 0.973  attractors 0.41

**The gist did it, not the collapse**: the opposite of my first reading of the
combined number. Embedding tier 4's summary pulls correct answers into the pool
that were not being retrieved at all, and displaces noise on the way in. The
blob collapse is worth keeping but is a smaller, structural effect.

MRR is flat across all of it. That is the finding, not a disappointment:
**recall@pool 0.973 with MRR 0.775 means the right answer is nearly always
retrieved and nearly never on top.** Ordering is the entire remaining problem,
which is what phase 3 was predicted to be for, and this is the evidence.

## Phase 2: `code` kinds enriched

111/113 enriched, up from 107. The two remaining are the JPEGs, which stay
unlabelled deliberately: the vision path on this box fails *open*: a non-vision
model accepts the multimodal payload with HTTP 200 and describes an image it
never received. Fabrication is worse than a blank, so it is left blank.

The four newly labelled files are HTML documents and a shell script, and their
labels are good ("Bash script to download 'A Trip Thru Time Fantastic' album
tracks and cover art"). Worth noting the failure was invisible from the outside:
they were counted as *processed*, not as skipped, because `t4_enriched` is set
either way. A coverage number that cannot distinguish "described" from
"declined to describe" will read as complete while it is not.

## Phase 3: `ask --smart`

    set          n     MRR  recall@5   hit@5  recall@pool  attractors
    all         22   0.890     0.890   1.000        0.973        0.32
    direct       6   1.000     1.000   1.000        1.000        0.33
    abstract    16   0.849     0.849   1.000        0.964        0.31

Against phase 1: MRR 0.775 -> 0.890, recall@5 0.742 -> 0.890, and **hit@5 0.864
-> 1.000**: every query in the set now puts a correct answer in the top five.
The gain is almost entirely in the abstract half (MRR 0.691 -> 0.849), which is
where it was predicted and where fusion alone had no answer.

The ship gate is met, so it ships, behind the flag.

### Latency, which I got wrong by an order of magnitude

Predicted 3-6s. Measured 36-52s end to end, of which 9-26s is model time; the
rest is process start, query embedding and Chroma. Worse, the *same* prompt
takes 11.6s and 26.0s on consecutive runs, so there is no stable figure to
quote: a reasoning model's generation length is the variable, and bonsai
ignores `enable_thinking: false`.

This is why `--smart` is opt-in and not the default. It is the right tool for a
question you cannot phrase and the wrong one for looking up a file you can name.

### A thing worth knowing about the pool

`hydrate()` collapses duplicate blobs *before* reranking, so a 30-deep fused
pool routinely reaches the model as ~10 distinct files. `MAX_CANDIDATES = 30` is
a ceiling that is rarely touched. This was invisible until profiling showed
identical prompt token counts for supposedly different candidate counts; the
lesson is that a knob whose effect never shows up in the numbers may not be
connected to anything.

## Not done, and deliberately

- **The two JPEGs stay unlabelled.** The vision path fails open on this box.
- **Phase 4 (category vocabulary) is untouched.** `category` now reaches the
  embedding but still drives no filter or ranking directly. Revisit on a mixed
  corpus, not on 113 files of dev notes.
- **No relevance floor beyond the reranker's judgement.** Attractors fell from
  0.82 to 0.32 without one; a hard floor can wait until something needs it.
