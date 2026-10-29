# bytecat

**"Is this drive backed up anywhere?"** Answered by content hash, across every host you
own, without a single file leaving the box.

bytecat catalogues every drive on every machine (the scanner is one stdlib-only file you can
`scp` anywhere), then answers the questions the duplicate finders don't:

- what on this USB drive exists **nowhere else**?
- what is duplicated across the NAS and the laptop?
- with a local model: *"where's that PDF about the mortgage?"*

Nothing is ever deleted. Nothing is ever uploaded. Proven on a catalogue of 825,000 files
across 10 TB and three hosts.

fclones, rmlint and czkawka find duplicates on one machine. bytecat is for the
person with a NAS, two laptops and a drawer of external drives who wants to know what is
safe to unplug.

## Install

Python 3.9+, nothing else to install:

```sh
pipx install git+https://github.com/keepitlit/bytecat     # gives you `bytecat`
# or, from a clone, with no install at all:
python3 -m bytecat --help
```

Optional, per tier (see below): `exiftool` for media metadata, `markitdown` for document
text, an OpenAI-compatible local model server (LM Studio, Ollama, llama.cpp) and Chroma for
semantic search. Tiers 0–1 (the coverage and duplicate answers) need none of them.

## Why it's shaped this way

The hard problem isn't understanding files, it's cost. Running a language model
over a million files is infeasible, so each tier runs on a strict subset of the
one before:

| Tier | What it does | Runs on |
|---|---|---|
| 0 walk | path, size, mtime, inode | everything (minutes) |
| 1 hash | size bucket → head/tail → full hash only in collision buckets | everything (I/O-bound) |
| 2 metadata | EXIF, media props, document properties | typed files (fast) |
| 3 text + vectors | markitdown → local embedding model → Chroma | documents |
| 4 LLM labels | title, gist, category for still-ambiguous files | **budgeted** |

Most files never get opened at all: if a file's size is unique across the whole
catalog it cannot have a duplicate, so tier 1 marks it done without reading a
byte. Tier 4 is ranked by how little we already know: a photo with clean EXIF
in `2024/Italy/` is skipped so the budget goes to `Scan_0043.pdf` in Downloads.

## Guarantees

- **Local only.** `llm.py` and `chroma.py` refuse any non-loopback endpoint, and
  there is no setting that relaxes it. If the model server is down the pipeline
  stops rather than falling back to anything hosted. The guard is re-applied to every
  HTTP redirect hop, because `urllib` follows 302s without asking.
- **Helpers are on a leash.** The invariant is only as strong as the subprocesses
  we shell out to, and those have their own network stacks. `extract_text()`
  routes to markitdown through an **allowlist** (`MARKITDOWN_EXTS`), never a
  fallback: markitdown transcribes `.wav/.mp3/.m4a/.mp4` by POSTing the raw audio
  to Google's speech API, so audio and video are refused outright and indexed by
  path and metadata instead. Never restore the catch-all.
- **Read-only.** Phase 1 never moves, renames, deletes, or touches mtimes. The
  duplicate report has no delete path and no `--apply` flag, by design.
- **Nothing duplicated.** Extracted text is embedded and discarded. What persists
  is a vector, a character count, and a checksum. Chroma's `documents` field is
  deliberately left null.
- **Offline is normal.** An unplugged drive marks its rows offline. It never
  looks like a mass deletion, and its files stay searchable.
- **Resumable.** Every tier resumes by asking "which rows are behind?", so a
  20-hour pass that dies at hour 19 restarts at hour 19.

## Where things live

Keep the data **out of any synced folder**: a churning multi-GB SQLite file inside a
Syncthing or Dropbox tree replicates to every peer continuously.

    data    ~/.local/share/bytecat/      (override with BYTECAT_HOME)
              catalog.db                 source of truth
              reports/                   duplicate / coverage / audit reports
    vectors Chroma on :8100, collection `bytecat_files`  (derived, rebuildable)

## Requirements by tier

| need | for | how |
|---|---|---|
| Python 3.9+ | everything | |
| `exiftool` | tier 2 (media metadata) | `brew install exiftool` / `apt install libimage-exiftool-perl` |
| `markitdown` | tier 3 (document text) | `pipx install "markitdown[all]"`, then see below |
| an OpenAI-compatible model server on loopback | tiers 3–4, `ask --smart` | LM Studio, Ollama, `llama-server`, vLLM … |
| Chroma | tier 3 vectors | `pipx install chromadb && ANONYMIZED_TELEMETRY=False chroma run --port 8100 --path ~/.local/share/bytecat/chroma` |

### Model server

Any server that speaks `/v1/embeddings` and `/v1/chat/completions` on `127.0.0.1`:

| variable | default | |
|---|---|---|
| `BYTECAT_LLM_BASE` | `http://127.0.0.1:1234/v1` | LM Studio's default; Ollama is `http://127.0.0.1:11434/v1` |
| `BYTECAT_EMBED_MODEL` | `text-embedding-nomic-embed-text-v1.5` | any embedding model your server has loaded |
| `BYTECAT_CHAT_MODEL` | *(unset)* | required for `enrich` and `ask --smart`; anything ~4B+ with JSON output works |
| `BYTECAT_LLM_AUTOSTART` | off | `1` lets bytecat run `lms server start` (LM Studio only) |
| `BYTECAT_CHROMA_BASE` | `http://127.0.0.1:8100` | |
| `BYTECAT_CHROMA_COLLECTION` | `bytecat_files` | |

Non-loopback values are refused; that is the invariant, not a default.

### Setting them up so they stay local

The guarantees above cover bytecat's own code. These three are separate programs
with their own network stacks, and two of them phone home unless told not to.

- **Chroma:** start it with `ANONYMIZED_TELEMETRY=False`. The stock server ships
  PostHog telemetry **on**. It sends collection commands and usage counts (not
  file names or contents), but it is still an off-box call, and bytecat cannot set
  the flag for you because it speaks HTTP to Chroma rather than importing it.
- **markitdown:** keep `SpeechRecognition` uninstalled from its venv:
  `uv pip uninstall --python ~/.local/share/uv/tools/markitdown/bin/python SpeechRecognition`.
  The allowlist in `extract.py` already stops bytecat handing it audio; removing
  the package makes the upload path absent rather than merely unreachable.
  ⚠️ `uv tool upgrade markitdown` reinstalls it, because `markitdown[all]`
  declares it; re-check after any upgrade.
- **Your model server** is a separate program. bytecat guarantees it dials loopback;
  what the server does with that request is its own business. LM Studio, for
  instance, is closed-source and makes its own calls for model search, downloads
  and update checks. To make this provable rather than trusted, watch the enrich
  pass with a firewall log, or pull the model and take the box off the network
  for the run.

## Use

```sh
# 1. index this machine
python3 -m bytecat scan ~/Documents ~/Pictures | python3 -m bytecat merge

# a host you can't mount locally: copy the one stdlib-only file over
scp bytecat/scan.py nas:/tmp/
ssh nas 'python3 /tmp/scan.py /mnt/user --host nas' | python3 -m bytecat merge

# an external drive: identity follows the volume UUID, not the mount point,
# so it survives remounting at "/Volumes/Backup 1"
python3 -m bytecat scan /Volumes/Backup | python3 -m bytecat merge

# 2. the cheap tiers (hash + metadata + vectors)
python3 -m bytecat pipeline

# 3. what's duplicated: a report, nothing is deleted
python3 -m bytecat dupes
python3 -m bytecat dupes --cross-host-only --min-bytes 10485760

# 4. search
python3 -m bytecat ask "insurance policy from 2024"
python3 -m bytecat ask "photos from the cabin trip" --kind image
python3 -m bytecat ask "tax return" --host nas

# 5. what exists nowhere else: the report to read before unplugging a drive
python3 -m bytecat coverage --source-volume "Backup 1" --target-host nas

# 6. cross-check a verdict against the disk itself, under a second algorithm
python3 -m bytecat audit coverage --source "/Volumes/Backup 1" --target /mnt/user/media
python3 -m bytecat audit dupes --path /mnt/user/media

# 7. optional: spend LLM time on the files we understand least
python3 -m bytecat enrich --budget 200
python3 -m bytecat enrich --budget 100 --vision     # images

python3 -m bytecat status
```

### Hashing a host you can't mount

Full hashing needs the bytes, so it happens on the host that has them. This is
demand-driven: a remote host only ever reads files that are genuinely
duplicate candidates:

```sh
python3 -m bytecat hash --emit-work nas > work.json
# run work.json on that host, return NDJSON
python3 -m bytecat hash --ingest results.ndjson
```

## Rescanning after a reorganisation

The hash cache is keyed on `(volume_id, inode, mtime, size)`, not on the path.
Move, rename or hardlink a file and its fingerprint is inherited from the row
that already held it; the bytes are never read again. This is what makes a
rescan after a big reorganisation affordable rather than a multi-day job.

```sh
python3 -m bytecat hash --no-inode-cache        # measure everything the slow way
python3 -m bytecat hash --forget --volume <uuid>  # drop tier-1 state, re-read it all
```

`--forget` requires `--volume`; without it the command is refused, because
forgetting the whole catalog would re-read every mounted drive.

**The one hazard, stated plainly.** Like every mtime-keyed cache (including the
path-keyed one bytecat has always had), a file whose bytes change while its mtime
does not will keep its old fingerprint. `rsync -t`, `tar` extraction and
`touch -r` all do this. `--forget` is the fix, and it is why that flag ships
with the cache rather than after it.

## Auditing a verdict

`bytecat audit` re-derives bytecat's riskiest claims from the disk instead of from
the catalog. It shares no code with the hashing path: its own directory walk,
its own read strategy, and SHA-256 rather than BLAKE2b-128.

- `audit coverage --source <path> --target <path>`: walks both trees and
  crosses "is this content on the target?" against bytecat's own answer. The
  off-diagonal cells are the product; a non-empty **bytecat says copied, disk says
  no** cell is a false "it's safely copied", the failure that would actually cost
  data.
- `audit dupes --path <path>`: re-reads only the members of duplicate groups
  and asserts SHA-256 partitions them exactly as `blob_id` does. A group that
  splits is a collision or a bug, and it is the check you want run *before* an
  `rm -r`, not after. Catalog-vs-disk drift falls out for free.

Both exit non-zero on any disagreement, so they can gate a script, and both
write the per-file listing to `~/.local/share/bytecat/reports/`; the terminal
gets the verdict first, then counts, never paths. Every report ends with what the run does and does not prove:
a different algorithm and a fresh read of real bytes, but not independence of
authorship. `--emit-manifest` writes the digests in hashdeep's manifest format
so a genuinely external tool can be pointed at the same tree.

## Bundles

`.photoslibrary`, `.lrlibrary`, `.app` and friends are single logical objects
whose internals are opaque and must not be reorganised. Each is recorded as
**one catalog entry** carrying its total size and internal file count, so your
Photos library shows up as "180 GB, 60,000 files" rather than disappearing.
Pass `--descend-bundles` to index their contents instead.

> On macOS, reading inside `Photos Library.photoslibrary` requires Full Disk
> Access for your terminal. Without it the scan reports the bundle at 0 bytes
> (the failure mode is `Interrupted system call`).

## Tests

```sh
tests/run_all.sh            # or any one: python3 tests/test_pipeline.py
```

Seeds a tree with known contents and asserts the properties that are easy to
break and expensive to notice: exclusion rules, idempotence, offline handling,
deletion marking, hardlink accounting, and the privacy invariant. Tier 3 checks
skip if the model server or Chroma is unavailable. Uses a temporary catalog and the
`bytecat_test` Chroma collection, so it never touches real state.

The other scripts follow the same shape, and each is built to fail if the
feature under test is inert: `test_inode_cache.py` renames files that *must* be
re-read without the cache and asserts the two modes disagree about the work
while agreeing about the answer; `test_audit.py` plants deliberate lies in a
temporary catalog and asserts the audit catches every one and exits non-zero.

## Measuring ranking changes

`bytecat eval` scores retrieval (MRR, recall@5, attractor rate) against a fixed query set.
The bundled set targets the small fictional corpus in `tests/fixtures/eval-corpus`:

```sh
export BYTECAT_HOME=$(mktemp -d)
python3 -m bytecat scan tests/fixtures/eval-corpus | python3 -m bytecat merge
python3 -m bytecat pipeline        # embed needs a model server; keyword-only otherwise
python3 -m bytecat eval --json before.json
# ... change the ranker ...
python3 -m bytecat eval --baseline before.json
```

Write your own `--queries` file against your real catalog to measure on real data; the
format is in `tests/eval_queries.json`. `docs/design/` holds the dated notes on how the
ranking pipeline got its shape.

## Notes

Running the audit on a NAS with no Python interpreter is written up in
[docs/hosts-without-python.md](docs/hosts-without-python.md).


- BLAKE2b, not BLAKE3: the scanner must run on hosts where nothing can be
  installed, and b2 is in the standard library. Disk I/O is the bottleneck
  regardless.
- Search degrades rather than fails. With Chroma or the model server down you
  still get keyword results over paths, names and any labels already stored.
- Enriching a file resets its tier-3 flag, so the next `embed` run folds the new
  title and gist into its vector.

## License

MIT, see `LICENSE`. Copyright (c) 2026 Keep It Lit Software Inc.
