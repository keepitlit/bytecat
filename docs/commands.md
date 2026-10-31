# Command reference

Every subcommand and flag, grouped by task. Run any command with `--help` for the same
list. The examples use `bytecat`; from a clone without installing, type `python3 -m bytecat`
instead.

Catalog state lives in `~/.local/share/bytecat/` (or `$BYTECAT_HOME`); reports go to its
`reports/` folder. No command deletes, moves or renames your files.

- [Index](#index): `scan`, `merge`, `hash`, `pipeline`, `extract`, `embed`, `enrich`
- [Find duplicates](#find-duplicates): `dupes`
- [Check coverage](#check-coverage): `coverage`
- [Search](#search): `ask`
- [Verify](#verify): `audit coverage`, `audit dupes`
- [Maintenance](#maintenance): `status`, `hash --forget`, `embed --reset`, `eval`
- [Environment variables](#environment-variables)
- [Exit codes](#exit-codes)

## Index

### scan

```
usage: bytecat scan [-h] [--host HOST] [--volume-uuid VOLUME_UUID]
                    [--allow-marker] [--include-hidden] [--descend-bundles]
                    [--min-size MIN_SIZE] [--progress-every PROGRESS_EVERY]
                    [--hash-list HASH_LIST] [--hash-root HASH_ROOT]
                    [roots ...]
```

Use it when you add a drive or a machine, and again whenever its files have changed. It
lists files as NDJSON on stdout and opens none of them; pipe it into `merge`.

`scan.py` is a single stdlib-only file. On a host where bytecat is not installed, copy
`bytecat/scan.py` over and run `python3 scan.py ...` with the same flags.

| flag | what it does |
|---|---|
| `roots` | Folders to list. Give each drive one root and keep using the same one: two roots on the same drive are stored as one drive, so a later scan of either marks the other's files missing. |
| `--host HOST` | Host name to record. Default: this machine's name, up to the first dot. |
| `--volume-uuid ID` | Use this ID for the drive instead of detecting one. For network shares, and for Linux drives you want to keep their identity across mount points. |
| `--allow-marker` | When the drive has no ID, write `.bytecat-volume-id` at its root once and use it from then on. The only flag that makes `scan` write anything. |
| `--include-hidden` | Also descend into folders whose names start with `.`. Hidden files in ordinary folders are listed either way. |
| `--descend-bundles` | List the files inside bundles (`.photoslibrary`, `.app`, `.lrlibrary` ...) instead of one entry per bundle. |
| `--min-size N` | Skip files smaller than N bytes. Default 1, which skips empty files. |
| `--progress-every N` | Print a progress line to stderr every N files. Default 20000; 0 turns it off. |
| `--hash-list FILE` | Instead of scanning, hash the files listed in a work list from `bytecat hash --emit-work` and print the results for `bytecat hash --ingest`. |
| `--hash-root DIR` | With `--hash-list`: read the listed files under DIR instead of the root they were scanned from, for example inside a container. |

How a drive is identified, in order: `--volume-uuid`; on macOS, the volume UUID from
`diskutil`; a `.bytecat-volume-id` marker at the drive root; otherwise
`synthetic:<host>:<mount point>`. On Linux `scan` does not read filesystem UUIDs, so an
external drive without `--allow-marker` or `--volume-uuid` becomes a new drive when it
mounts somewhere else.

Always skipped: build and package folders (`node_modules`, `.git`, `target`, `venv` ...),
OS scratch (`.Trashes`, `.Spotlight-V100`, `System Volume Information` ...), `.DS_Store`,
`Thumbs.db`, and temporary or compiled files (`.tmp`, `.part`, `.pyc` ...). The full lists are
`SKIP_DIRS`, `SKIP_FILES` and `SKIP_EXTS` in `bytecat/scan.py`. Symlinks are recorded and
never followed.

### merge

```
usage: bytecat merge [-h] [--no-prune] [path]
```

Use it on the output of every `scan`. Adds new files, updates changed ones, and marks files
the scan did not see as missing. Rows are never deleted. Only drives present in the input are
touched, so a drive you did not scan this time is left as it was.

| flag | what it does |
|---|---|
| `path` | NDJSON file to read. Default: stdin. |
| `--no-prune` | Do not mark unseen files as missing. Use it when the scan covered only part of a drive. |

### hash

```
usage: bytecat hash [-h] [--volume VOLUME] [--workers WORKERS] [--limit LIMIT]
                    [--emit-work HOST] [--hash-all] [--no-inode-cache]
                    [--forget] [--ingest NDJSON]
```

Use it to find out which files are identical (tier 1). `pipeline` runs it for you. It reads
every catalogued drive whose scan root exists on this machine. A file whose size no other
file in the catalog shares is marked done without being read; files that share a size are
hashed with BLAKE2b.

| flag | what it does |
|---|---|
| `--volume VOLUME` | Restrict to one drive: its UUID, label or mount, matched exactly. |
| `--workers N` | Parallel readers. Default: the CPU count, at most 8. |
| `--limit N` | Hash at most N files this run. |
| `--emit-work HOST` | Print the work list of files HOST must hash (JSON, to stdout) instead of hashing. |
| `--ingest NDJSON` | Load the results of `scan.py --hash-list` run on another host. |
| `--hash-all` | Also hash files with a unique size, so every file gets a fingerprint. With `--forget`, files already skipped are read too. |
| `--no-inode-cache` | Do not reuse fingerprints for moved, renamed or hard-linked files; read them again. |
| `--forget` | Clear the fingerprints of the drive named by `--volume` and hash it again. Refused without `--volume`. |

A host you cannot reach from this machine hashes its own files:

```sh
bytecat hash --emit-work nas > work.json
scp work.json bytecat/scan.py nas:/tmp/
ssh nas 'cd /tmp && python3 scan.py --hash-list work.json' > results.ndjson
bytecat hash --ingest results.ndjson
```

### pipeline

```
usage: bytecat pipeline [-h] [--limit LIMIT]
```

Use it after merging new scans. Runs `hash`, `extract` and `embed` in that order and stops at
the first step that fails; re-running resumes where it stopped. `enrich` is never part of it.
Without a model server and Chroma, `embed` fails and the pipeline exits 1 after `hash` and
`extract` have finished, which is all `dupes` and `coverage` need.

| flag | what it does |
|---|---|
| `--limit N` | Passed to `extract` and `embed` as their `--limit`. `hash` is not limited. |

### extract

```
usage: bytecat extract [-h] [--limit LIMIT]
```

Use it to record each file's kind (document, image, video ...) and, for images, video and
audio, metadata from `exiftool` such as the date taken (tier 2). `ask --kind` depends on it.
Without `exiftool` it warns once and records kinds only.

| flag | what it does |
|---|---|
| `--limit N` | Describe at most N files this run. |

### embed

```
usage: bytecat embed [-h] [--limit LIMIT] [--kinds KINDS] [--reset]
```

Use it to make files findable by meaning (tier 3). Every file on a drive mounted here is
embedded from its path and name, plus its text if it has any: plain-text, code and data files
are read directly, and PDF, Office, iWork and e-book files go through `markitdown`. Audio and
video are never sent to `markitdown`. The text is discarded after embedding. Needs a model
server and Chroma on this machine; see the README.

| flag | what it does |
|---|---|
| `--limit N` | Embed at most N files this run. |
| `--kinds KINDS` | Only these kinds, comma-separated: `archive,audio,bundle,code,data,document,image,other,video`. |
| `--reset` | Delete every vector in the collection, then re-embed files on drives mounted now. |

### enrich

```
usage: bytecat enrich [-h] [--budget BUDGET] [--kinds KINDS] [--vision]
```

Use it to give hard-to-find files a title and a one-line summary from a local chat model (tier
4). Files the catalog knows least about go first: generic names like `Scan_0043.pdf`, no
title, dumping-ground folders. Needs `BYTECAT_CHAT_MODEL`. Run `embed` afterwards so search
uses the new labels.

| flag | what it does |
|---|---|
| `--budget N` | Label at most N files. Default 200; must be positive. |
| `--kinds KINDS` | Only these kinds, comma-separated. Default: `document,data,other,code`. |
| `--vision` | Label images by sending each one to the model. `BYTECAT_CHAT_MODEL` must accept images. |

## Find duplicates

### dupes

```
usage: bytecat dupes [-h] [--min-bytes MIN_BYTES] [--cross-host-only]
                     [--top TOP] [--out OUT]
```

Use it to see what is stored more than once and how much space one copy of each would free.
The terminal gets totals; every path goes to a Markdown report. Hard links to one file count
as one copy. Copies on an unplugged drive are listed and marked `(volume offline)`. There is
no delete option.

| flag | what it does |
|---|---|
| `--min-bytes N` | Leave out groups that would free less than N bytes. Default 1. |
| `--cross-host-only` | Only groups with copies on more than one machine. |
| `--top N` | Groups to list per section. Default 500. |
| `--out PATH` | Write the report here instead of `reports/duplicates-<date>-<time>.md`. |

## Check coverage

### coverage

```
usage: bytecat coverage [-h] --source-volume SOURCE_VOLUME
                        [--target-volume TARGET_VOLUME]
                        [--target-host TARGET_HOST] [--out OUT]
```

Use it before you wipe or retire a drive: it lists every file on the source whose content is
on none of the targets, wherever and under whatever name the targets hold it. Run `hash`
first. The terminal gets totals; the paths go to a Markdown report.

| flag | what it does |
|---|---|
| `--source-volume DRIVE` | The drive to check: its UUID, label or mount, matched exactly. Required. |
| `--target-volume DRIVE` | A drive that counts as a copy. Repeat for more. |
| `--target-host HOST` | Every drive ever scanned on HOST counts as a copy, plugged in or not. Repeat for more. |
| `--out PATH` | Write the report here instead of `reports/coverage-<date>-<time>.md`. |

At least one target is required, and the source is never its own target. The report sorts
each file into one of:

- **Not on any target**: the content is on no target. `hashed` means it was read and matched
  nothing; `by size` means no other file in the catalog has its size, so it was never read.
- **Not hashed yet**: `hash` has not reached it; no claim either way.
- **On a target (by content)**.

## Search

### ask

```
usage: bytecat ask [-h] [--limit LIMIT]
                   [--kind {archive,audio,bundle,code,data,document,image,other,video}]
                   [--host HOST] [--online-only] [--full-path] [--smart]
                   query [query ...]
```

Use it to find a file by its name, path or what it is about. Keyword search over paths, names
and any labels works from the first `merge`; with a model server and Chroma, results by
meaning are blended in. Identical copies are shown once with a copy count. Prints paths and
metadata, never file contents.

| flag | what it does |
|---|---|
| `query` | Words to search for. |
| `--limit N` | Results to show. Default 20. |
| `--kind KIND` | Only files of this kind. Needs `extract` to have run. |
| `--host HOST` | Only files on this host. |
| `--online-only` | Hide files on offline drives. |
| `--full-path` | Show the full path for files on other machines too, not just `host:drive  path`. |
| `--smart` | Re-rank the results with the chat model in `BYTECAT_CHAT_MODEL`. Slower; skipped with a notice when no model answers. |

## Verify

`audit` re-checks a `coverage` or `dupes` verdict against the disk with its own directory
walk and SHA-256. Each path you give must be a folder on this machine at or under a catalogued
drive's scan root. Per-file detail goes to `reports/audit-coverage-<date>-<time>.md` or
`reports/audit-dupes-<date>-<time>.md`. Both exit 1 when the disk and the catalog disagree.

### audit coverage

```
usage: bytecat audit coverage [-h] --source SOURCE --target TARGET
                              [--workers WORKERS] [--out OUT]
                              [--emit-manifest PATH]
```

Use it before deleting a drive's contents on the strength of a `coverage` report. It reads
every file under both folders and compares what is really on the target with what bytecat
says. A file bytecat called copied that the disk says is not is the finding that matters.

| flag | what it does |
|---|---|
| `--source PATH` | The folder being retired. Required. |
| `--target PATH` | The folder that should hold its copies. Required; must differ from the source. |
| `--workers N` | Parallel readers. Default: the CPU count, at most 8. |
| `--out PATH` | Write the report here. |
| `--emit-manifest PATH` | Also write SHA-256 digests in hashdeep format, as `<name>-source` and `<name>-target` next to PATH, for checking with an outside tool. |

### audit dupes

```
usage: bytecat audit dupes [-h] --path PATH [--min-fanout MIN_FANOUT]
                           [--no-drift] [--workers WORKERS] [--out OUT]
                           [--emit-manifest PATH]
```

Use it before deleting copies on the strength of a `dupes` report. It re-reads every member of
each duplicate group under PATH and checks SHA-256 groups them exactly as bytecat did.

| flag | what it does |
|---|---|
| `--path PATH` | Folder to check. Required. |
| `--min-fanout N` | Only groups with at least N members under PATH. Default 2. |
| `--no-drift` | Skip listing files added, removed or resized since the scan. |
| `--workers N` | Parallel readers. Default: the CPU count, at most 8. |
| `--out PATH` | Write the report here. |
| `--emit-manifest PATH` | Also write the SHA-256 digests in hashdeep format. |

## Maintenance

### status

```
usage: bytecat status [-h]
```

Use it to see what is catalogued and how far each tier has got. Lists every drive by host
with its scan root, label, ID, file count and scan date, and `offline` for this machine's drives
that are not mounted now. Progress rows are named after the command that advances them. The
last five runs are listed, in local time.

### Re-reading a drive: `hash --forget`

`bytecat hash --forget --volume DRIVE` clears one drive's fingerprints and reads it again.
Use it when files may have changed without their modification time changing (`rsync -t`,
`tar` extraction, `touch -r`).

### Rebuilding search vectors: `embed --reset`

`bytecat embed --reset` empties the Chroma collection and re-embeds files on drives mounted
now. Files on other drives stay findable by keyword until their drive is mounted and
`embed` runs again.

### eval

```
usage: bytecat eval [-h] [--json JSON] [--baseline BASELINE] [--smart]
                    [--queries QUERIES]
```

Use it when changing how search ranks results. Scores retrieval (MRR, recall@5, hit@5,
recall@pool, attractor rate) against a fixed query set.

| flag | what it does |
|---|---|
| `--json PATH` | Write the results to PATH. |
| `--baseline PATH` | Show the MRR change against a saved `--json` run. |
| `--smart` | Score with `ask --smart` re-ranking. |
| `--queries PATH` | Query set. Default: the set bundled with bytecat, for `tests/fixtures/eval-corpus`. |

## Environment variables

| variable | default | used by |
|---|---|---|
| `BYTECAT_HOME` | `~/.local/share/bytecat` | everything: the catalog and `reports/` |
| `BYTECAT_LLM_BASE` | `http://127.0.0.1:1234/v1` | `embed`, `enrich`, `ask`, `eval` |
| `BYTECAT_EMBED_MODEL` | `text-embedding-nomic-embed-text-v1.5` | `embed`, `ask` |
| `BYTECAT_CHAT_MODEL` | unset | `enrich`, `ask --smart`, `eval --smart` |
| `BYTECAT_LLM_AUTOSTART` | unset | `1` lets bytecat run `~/.lmstudio/bin/lms server start` when no server answers |
| `BYTECAT_CHROMA_BASE` | `http://127.0.0.1:8100` | `embed`, `ask` |
| `BYTECAT_CHROMA_COLLECTION` | `bytecat_files` | `embed`, `ask` |

The model and Chroma addresses must be loopback (`127.0.0.1`, `localhost`, `::1` or
`0.0.0.0`); anything else is refused.

## Exit codes

| code | meaning |
|---|---|
| 0 | Done. `status` also exits 0 when there is no catalog yet. |
| 1 | `audit` found a disagreement; or `embed`, `enrich` or a `pipeline` step failed. |
| 2 | A flag or argument is wrong; no catalog yet (`ask`, `dupes`, `coverage`, `audit`, `eval`); `hash --forget` without `--volume`; `enrich --budget` not positive; `ask --host` names an unknown host. |

Errors from `sys.exit` with a message (an unknown drive, an uncatalogued audit path, a
missing target) also exit 1.
