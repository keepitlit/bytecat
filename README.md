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
person with a NAS and a drawer of external drives who wants to know what is
safe to unplug.

**Start here:** [docs/quickstart.md](docs/quickstart.md) goes from install to "what on
this drive exists nowhere else?" with real output at every step.
[docs/commands.md](docs/commands.md) lists every command and flag.

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
over a million files is infeasible, so each tier reads as little as it can, and
only the last one runs a language model, on a budget:

| Tier | What it does | Runs on |
|---|---|---|
| 0 walk | path, size, mtime, inode | everything (minutes) |
| 1 hash | size bucket → head/tail → full hash only in collision buckets | everything (I/O-bound) |
| 2 metadata | file kind; EXIF and media properties via exiftool | everything; exiftool only on photos, audio, video |
| 3 text + vectors | path, name and text (markitdown for documents) → local embedding model → Chroma | everything; text only from documents, code and data |
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
  duplicate report has no delete path and no `--apply` flag, by design. The one
  write to a scanned tree is `scan --allow-marker`, which, when asked, puts a
  `.bytecat-volume-id` file at the root of a drive that has no ID of its own.
- **Nothing duplicated.** Extracted text is embedded and discarded. What persists
  is a vector, a character count, and a checksum. Chroma's `documents` field is
  deliberately left null.
- **Offline is normal.** An unplugged drive shows as offline in `status`, `ask` and
  the duplicate report, and the tiers skip it. It never looks like a mass deletion, and
  its files stay searchable. Another host's drives show the state of their last scan.
- **Resumable.** Every tier resumes by asking "which rows are behind?", so a
  20-hour pass that dies at hour 19 restarts at hour 19.

## Where things live

Keep the data **out of any synced folder**: a churning multi-GB SQLite file inside a
Syncthing or Dropbox tree replicates to every peer continuously.

    data    ~/.local/share/bytecat/      (override with BYTECAT_HOME)
              catalog.db                 source of truth
              reports/                   duplicate / coverage / audit reports
    vectors Chroma on :8100, collection `bytecat_files`  (derived, rebuildable)

To uninstall: `pipx uninstall bytecat`, then delete the data directory. Drives scanned
with `--allow-marker` keep a `.bytecat-volume-id` file at their root, and Chroma keeps
the `bytecat_files` collection in its own data path.

## Requirements by tier

| need | for | how |
|---|---|---|
| Python 3.9+ | everything | |
| `exiftool` | tier 2 (media metadata) | `brew install exiftool` / `apt install libimage-exiftool-perl` |
| `markitdown` | tier 3 (document text) | `pipx install "markitdown[all]"`, then see below |
| an OpenAI-compatible model server on loopback | tiers 3–4, `ask --smart` | LM Studio, Ollama, `llama-server`, vLLM … |
| Chroma | tier 3 vectors | `pipx install chromadb && ANONYMIZED_TELEMETRY=False chroma run --port 8100 --path ~/.local/share/bytecat/chroma` |

### Model server

Any server on this machine that speaks `/v1/models`, `/v1/embeddings` and
`/v1/chat/completions`. bytecat checks `/v1/models` first and stops if it lists no model:

| variable | default | |
|---|---|---|
| `BYTECAT_LLM_BASE` | `http://127.0.0.1:1234/v1` | LM Studio's default; Ollama is `http://127.0.0.1:11434/v1` |
| `BYTECAT_EMBED_MODEL` | `text-embedding-nomic-embed-text-v1.5` | any embedding model your server has loaded |
| `BYTECAT_CHAT_MODEL` | *(unset)* | required for `enrich` and `ask --smart`; anything ~4B+ with JSON output works |
| `BYTECAT_LLM_AUTOSTART` | off | `1` lets bytecat run `lms server start` (LM Studio only) |
| `BYTECAT_CHROMA_BASE` | `http://127.0.0.1:8100` | |
| `BYTECAT_CHROMA_COLLECTION` | `bytecat_files` | |

Only `127.0.0.1`, `localhost`, `::1` and `0.0.0.0` are accepted; anything else is refused.
That is the invariant, not a default.

### Setting them up so they stay local

The guarantees above cover bytecat's own code. These three are separate programs
with their own network stacks, and two of them phone home unless told not to.

- **Chroma:** start it with `ANONYMIZED_TELEMETRY=False`. The stock server ships
  PostHog telemetry **on**. It sends collection commands and usage counts (not
  file names or contents), but it is still an off-box call, and bytecat cannot set
  the flag for you because it speaks HTTP to Chroma rather than importing it.
- **markitdown:** keep `SpeechRecognition` uninstalled from its venv:
  `pipx runpip markitdown uninstall -y SpeechRecognition` (installed with `uv tool`:
  `uv pip uninstall --python ~/.local/share/uv/tools/markitdown/bin/python SpeechRecognition`).
  The allowlist in `extract.py` already stops bytecat handing it audio; removing
  the package makes the upload path absent rather than merely unreachable.
  Upgrading markitdown reinstalls it, because `markitdown[all]` declares it;
  re-check after any upgrade.
- **Your model server** is a separate program. bytecat guarantees it dials loopback;
  what the server does with that request is its own business. LM Studio, for
  instance, is closed-source and makes its own calls for model search, downloads
  and update checks. To make this provable rather than trusted, watch the enrich
  pass with a firewall log, or pull the model and take the box off the network
  for the run.

## Use

```sh
# 1. index this machine: one root per drive, and the same root every time
python3 -m bytecat scan ~ | python3 -m bytecat merge

# a host you can't mount locally: copy the one stdlib-only file over
scp bytecat/scan.py nas:/tmp/
ssh nas 'python3 /tmp/scan.py /mnt/user --host nas' | python3 -m bytecat merge

# an external drive: on macOS its identity is the volume UUID, not the mount
# point, so it survives remounting at "/Volumes/Backup 1". On Linux add
# --allow-marker the first time, or it is identified by its mount point.
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
python3 -m bytecat coverage --source-volume Backup --target-host nas

# 6. cross-check a verdict against the disk itself, under a second algorithm;
#    both paths must be readable here, at or under where they were scanned
python3 -m bytecat audit coverage --source /Volumes/Backup --target ~/Pictures
python3 -m bytecat audit dupes --path ~/Pictures

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
scp work.json bytecat/scan.py nas:/tmp/
ssh nas 'cd /tmp && python3 scan.py --hash-list work.json' > results.ndjson
python3 -m bytecat hash --ingest results.ndjson
```

If the files sit under a different path on the host than the one scanned (a
container bind mount, say), add `--hash-root <that path>` to the `scan.py` line.

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
> Access for your terminal (System Settings, Privacy & Security). Without it the
> scan reports the bundle at 0 bytes (the failure mode is `Interrupted system call`);
> grant it and scan again.

## Troubleshooting

**`no catalog at …; start with: bytecat scan ~/Documents | bytecat merge`.** Nothing has
been merged into this catalog yet, or `BYTECAT_HOME` points somewhere other than where
you merged.

**`pipeline` ends with `embed failed`, after `no model server or model at
http://127.0.0.1:1234/v1`.** No model server is running, or it lists no model. `hash` and
`extract` have already finished, so `dupes`, `coverage`, `audit` and keyword `ask` work.
For search by meaning, start a server with an embedding model loaded, start Chroma, and
re-run `pipeline`.

**`Chroma not reachable at http://127.0.0.1:8100`** from `embed`, or `no Chroma at …`
from `ask`. Start Chroma as shown under Requirements by tier, or set
`BYTECAT_CHROMA_BASE`.

**`refusing non-loopback model endpoint '…'`** (or `Chroma endpoint`). The model server
and Chroma must run on this machine. No setting allows a remote one.

**`BYTECAT_CHAT_MODEL not set`, or `set BYTECAT_CHAT_MODEL to a model your server has
loaded`.** `enrich` and `ask --smart` need a chat model; set the variable to a model name
from your server's `/v1/models`.

**`no catalogued volume is mounted here; remote hosts: --emit-work HOST`.** `hash` reads
only drives whose scan root exists on this machine. Plug the drive in, or hash another
host's files there (see "Hashing a host you can't mount").

**`no volume 'X'; bytecat status lists them`, or `'X' matches 2 volumes`.** Drives are
named by UUID, label or scan root, matched exactly. Copy one from `bytecat status`; when two
drives share a label, pass the UUID.

**`<path> not catalogued; scan first`** from `audit`. The path must be at or under the
folder a drive was scanned from, as the catalog recorded it. A drive scanned at
`/Volumes/Backup` and now mounted at `/Volumes/Backup 1` needs a rescan first.

**A drive shows `offline` in `status` but is plugged in.** It is mounted somewhere other
than its last scan root; scan it again from where it is now. On macOS, a drive on this
machine scanned with `--volume-uuid` always reads as offline, because `diskutil` reports a
different ID.

**`scan` reports `N paths unreadable`.** Those folders could not be listed, usually for
permissions, and their files are not in the catalog. `bytecat scan <root> 2>/dev/null |
grep '"type": "error"'` lists them.

**`warning: exiftool not found; media metadata will be empty`.** Install `exiftool` and
run `bytecat extract` for dates and camera details on photos and video. Duplicates and
coverage do not need it.

**The hash row in `status` stays below 100% after hashing another host.** Files on a
host this machine cannot read are hashed only when `--emit-work` lists them, and a file
with a unique size is not listed, so a coverage report with that host's drive as the
source puts it under "Not hashed yet". `bytecat hash --emit-work <host> --hash-all` lists
those files too, and the host reads them.

**Removing a drive or host.** No command does this. A drive you have wiped or thrown
away keeps its files in the catalog, and they still count as copies for `--target-host`;
name the drives you trust with `--target-volume` instead.

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
`test_coverage.py` seeds files that are and are not on the target and asserts
each lands in the right bucket; `test_hash_all.py` asserts `--hash-all` reads the
size-unique files tier 1 skips and changes nothing else; `test_copy_budget.py`
holds every message bytecat prints to one sentence of at most 12 words.

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
