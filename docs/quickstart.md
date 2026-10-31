# Quickstart: what on this drive exists nowhere else?

This walk-through takes you from install to a list of the files on a USB drive that are on
no other machine, then checks that list against the disk.

Every block below is real output from one run on synthetic files: a laptop, a NAS whose share
is called `share`, and a USB drive called `OLD-USB` plugged into the laptop. The run was on
one machine, so `BYTECAT_HOST=laptop` and `--host nas` stand in for the two machines, and no
model server was running. Paths under `~/demo` are the demo's own folders.

## 1. Install

```sh
pipx install git+https://github.com/keepitlit/bytecat
```

Python 3.9 or later is the only requirement for everything on this page. From a clone you
can skip installing and type `python3 -m bytecat` wherever this page says `bytecat`.

The catalog is one SQLite file in `~/.local/share/bytecat/`. To try bytecat on a throwaway
catalog first, point `BYTECAT_HOME` somewhere else; the demo used `~/demo/bytecat-data`, and
named its machine `laptop` with `BYTECAT_HOST`:

```text
$ export BYTECAT_HOME=~/demo/bytecat-data BYTECAT_HOST=laptop
$ bytecat status
no catalog at ~/demo/bytecat-data/catalog.db; start with: bytecat scan ~/Documents | bytecat merge
```

## 2. Index this machine

`scan` lists files without opening them; `merge` stores the list in the catalog.

```text
$ bytecat scan ~/demo/laptop | bytecat merge
[bytecat] scanned 11 files; 0 paths unreadable
[bytecat] merged 11 files, 1 volume(s); 0 now missing; 0 paths unreadable
```

On your own machine leave out `BYTECAT_HOST`; the host defaults to the machine's name.

Scan the same folders each time they change. Several folders on one drive are fine: a
rescan of one leaves the others as they were.

A Photos library or other bundle is stored as one entry with its total size; in the demo,
`Pictures/Photos Library.photoslibrary` is one of the 11 files. On macOS, scanning inside your
real Photos library needs Full Disk Access for your terminal.

## 3. Index the USB drive

Plug it in and scan it from the machine it is plugged into:

```text
$ bytecat scan /Volumes/OLD-USB | bytecat merge
[bytecat] scanned 13 files; 0 paths unreadable
[bytecat] merged 13 files, 1 volume(s); 0 now missing; 0 paths unreadable
```

The drive is identified by its filesystem UUID, so it is the same drive wherever it
mounts.

## 4. Index the NAS

The scanner is one file with no dependencies, so it runs on the NAS itself and only the list
of files crosses the network:

```sh
scp bytecat/scan.py nas:/tmp/
ssh nas 'python3 /tmp/scan.py /mnt/user --host nas' | bytecat merge
```

In the demo, a copy of `scan.py` ran against the NAS folder:

```text
$ python3 ~/demo/scan.py ~/demo/nas --host nas | bytecat merge
[bytecat] scanned 12 files; 0 paths unreadable
[bytecat] merged 12 files, 1 volume(s); 0 now missing; 0 paths unreadable
```

A network share or a pooled array such as Unraid's `/mnt/user` has no filesystem UUID, so
it is identified by host name and mount point, which is stable as long as neither changes.
Pass `--volume-uuid <any name>` to pin it, or `--allow-marker` to write a small
`.bytecat-volume-id` file at its root, the one exception to `scan` being read-only. The
demo's NAS folder is a disk image, so it has a UUID of its own.

## 5. Hash the NAS's files on the NAS

Duplicates and coverage are decided by content, so files must be hashed where their bytes
are. Ask the catalog which NAS files need it:

```text
$ bytecat hash --emit-work nas > work.json
[bytecat] 12 files to hash on nas
```

Only files that share a size with some other file in the catalog are listed; a file with a
unique size cannot be a copy of anything, so it is marked done without being read. In the demo every NAS file shares a size, so all
12 are listed.

Run the list on the NAS with the same `scan.py`, then load the results:

```sh
scp work.json nas:/tmp/
ssh nas 'python3 /tmp/scan.py --hash-list /tmp/work.json' > results.ndjson
```

```text
$ python3 ~/demo/scan.py --hash-list work.json > results.ndjson
[bytecat] hashed 12 files; 0 unreadable
$ bytecat hash --ingest results.ndjson
[bytecat] ingested 12 hashes
```

`bytecat hash` never reads another host's drive from this machine, even where the same path
exists here, so this step is the only way the NAS's files get hashed.

## 6. Run the cheap tiers

```text
$ bytecat pipeline

=== hash ===
[bytecat] hashing 18 files
[bytecat] hashed 18; reused 0; 6 need no read; 0 unreadable

=== extract ===
[bytecat] media 13/13
[bytecat] described 24 files

=== embed ===
[bytecat] embed skipped: no model server or model at http://127.0.0.1:9/v1; start one or set BYTECAT_LLM_BASE
```

`hash` read the 18 laptop and USB files that share a size with another file; the 6 with
unique sizes needed no read. `extract` described the 24 files on this machine's drives: each
one's kind, plus photo, music and video metadata when `exiftool` is installed. The NAS's 12
files stay undescribed, as `status` shows below, because their bytes are on the NAS.

`embed` was skipped because no model server answered; the pipeline still exits 0. The demo
pointed `BYTECAT_LLM_BASE` at a closed port; without that setting the message names
`http://127.0.0.1:1234/v1`. Embedding is only for search by meaning: everything else on this
page needs `hash` alone. To add it later, see "Requirements by tier" in the
[README](../README.md) and run `bytecat pipeline` again. `bytecat pipeline --no-embed` stops
after `extract` without looking for a server.

```text
$ bytecat status
files:   36 present  (0.0 GB), 0 missing

laptop
  /Volumes/OLD-USB  OLD-USB  630F7B39-0A79-3806-BC86-672C5BC2D103  13 files  scanned 2026-10-30
  ~/demo/laptop  Macintosh HD  9EE1F9A1-0EB2-32F5-9FC6-32B5955014EB  11 files  scanned 2026-10-30
nas
  ~/demo/nas  share  E1FC116C-1A2E-3F7C-97D4-70526CA1A84C  12 files  scanned 2026-10-30

progress:
  hash               36 / 36  (100.0%)
  extract            24 / 36  (66.7%)
  embed               0 / 36  (0.0%)
  enrich              0 / 36  (0.0%)

duplicates: 10 groups, 0.0 GB reclaimable

recent runs:
  2026-10-30 20:20:29  extract  ok      24 seen
  2026-10-30 20:20:29  hash     ok      18 seen
  2026-10-30 20:20:28  merge    ok      12 seen
  2026-10-30 20:20:27  merge    ok      13 seen
  2026-10-30 20:20:27  merge    ok      11 seen
```

Each drive is listed under the host it was scanned from, with its scan root, label and ID.
A drive of this machine's that is unplugged shows `offline` at the end of its line and keeps
its files in the catalog. Each progress row is named after the command that advances it.

## 7. What is duplicated?

```text
$ bytecat dupes
[bytecat] 10 duplicate groups · 841.8 KB reclaimable · 8 span multiple machines (771.7 KB)
[bytecat] report written to ~/demo/bytecat-data/reports/duplicates-20261030-202029.md
```

The terminal gets totals only; open the report for the paths. Two groups from it:

```markdown
### 62.6 KB × 4 copies (187.9 KB reclaimable)

`b17dd49256536f80cb48e07b3389a469` · hosts: laptop, nas

- `laptop` [Macintosh HD] `Pictures/2019/italy/IMG_0412.jpg`
- `laptop` [OLD-USB] `Photos/2019/Italy/IMG_0412.jpg`
- `nas` [share] `photos/2019/italy/IMG_0412 (1).jpg`
- `nas` [share] `photos/2019/italy/IMG_0412.jpg`

### 47.1 KB × 2 copies (47.1 KB reclaimable)

`998d9c61d989101bd935f7330ddd92ed` · hosts: laptop, nas

- `laptop` [Macintosh HD] `Documents/taxes/2023-return.pdf`  _(hardlink)_
- `laptop` [Macintosh HD] `Desktop/2023-return.pdf`  _(hardlink)_
- `nas` [share] `backup/laptop/Documents/taxes/2023-return.pdf`
```

Each line is host, `[drive label]`, then the path inside that drive. The two laptop names in
the second group are hard links to one file, so they count as one copy. bytecat deletes
nothing; which copy to keep is your decision.

## 8. What on this drive exists nowhere else?

Name the drive by its label, UUID or mount, and the machine that should hold the copies:

```text
$ bytecat coverage --source-volume OLD-USB --target-host nas
[bytecat] not on target                 7 files    324.3 KB
[bytecat]   of which never read         3 files    176.5 KB
[bytecat] on target                     6 files    473.1 KB
[bytecat] report written to ~/demo/bytecat-data/reports/coverage-20261030-202029.md
```

The report lists every file that is not on the NAS:

```markdown
## Every file not on a target

| Path | Bytes | Why |
|---|---:|---|
| Photos/2017/wedding/DSC_1004.jpg | 62,500 | by size |
| Photos/2019/Italy/IMG_0413.jpg | 61,877 | hashed |
| Photos/2017/wedding/DSC_1002.jpg | 59,904 | by size |
| Photos/2017/wedding/DSC_1001.jpg | 58,331 | by size |
| Photos/2017/wedding/DSC_1003.jpg | 57,600 | hashed |
| Documents/old-cv-2016.docx | 22,016 | hashed |
| Documents/budget-2025.xlsx | 9,870 | hashed |
```

`hashed` means the file was read and its content matched nothing on the NAS.
`DSC_1003.jpg` has the same size as a different photo on the NAS, and `old-cv-2016.docx`
differs by one byte from the NAS's `cv-2016.docx`. `by size` means no other file in the
catalog has that size, so it cannot have a copy and was never read.

Matching is by content, not name: the drive's `Videos/holiday-2018.mp4` counts as on the NAS
because the NAS holds the same bytes as `media/movies/Holiday 2018 (edited).mp4`. A
`Not hashed yet` section appears when `hash` has not finished; those files have no answer
either way.

Add a target to see what is on neither machine:

```text
$ bytecat coverage --source-volume OLD-USB --target-host nas --target-host laptop
[bytecat] not on target                 5 files    254.2 KB
[bytecat]   of which never read         3 files    176.5 KB
[bytecat] on target                     8 files    543.2 KB
[bytecat] report written to ~/demo/bytecat-data/reports/coverage-20261030-202029-2.md
```

`IMG_0413.jpg` and `budget-2025.xlsx` are on the laptop, so 5 files remain: copy those
before you wipe the drive. The source drive never counts as its own copy. `--target-host`
counts every drive ever scanned on that host, unplugged ones included; to count only
particular drives, name each with `--target-volume`.

## 9. Check the answer against the disk

`coverage` is computed from the catalog. Before you delete anything, `audit coverage` reads
both folders again, hashes them with SHA-256, and compares:

```text
$ bytecat audit coverage --source /Volumes/OLD-USB --target ~/demo/nas
[bytecat] audit: reading source: 13 files (797.4 KB)
[bytecat] audit: reading target: 12 files (672.5 KB)
[bytecat] audit: catalog confirmed
[bytecat]   agree, content present on target             :         6
[bytecat]   not on target, bytecat did not say copied    :         7
[bytecat]   DISAGREE, bytecat says copied, disk says no  :         0
[bytecat]   DISAGREE, disk has it, bytecat did not say so:         0
[bytecat]   on disk, not in catalog (source)             :         0
[bytecat]   in catalog, not found or not read (source)   :         0
[bytecat] report written to ~/demo/bytecat-data/reports/audit-coverage-20261030-202030.md
```

`catalog confirmed` is the verdict. A non-zero `DISAGREE, bytecat says copied, disk says no`
row means the catalog would have let you delete a file that has no copy; the command then
exits 1 and the report lists those files first.

Both folders must be readable on the machine running the audit, at or under the path each
drive was scanned from. To audit on a NAS with no Python, see
[hosts-without-python.md](hosts-without-python.md).

The same check for a duplicate report re-reads every group under one folder:

```text
$ bytecat audit dupes --path ~/demo/nas
[bytecat] audit: reading duplicate groups: 2 files (125.2 KB)
[bytecat] audit: groups confirmed
[bytecat]   duplicate groups                        :         1  (2 files re-read)
[bytecat]   GROUPS THAT SPLIT                       :         0
[bytecat]   duplicates bytecat MISSED               :         0
[bytecat]   on disk, not in catalog                 :         0
[bytecat]   in catalog, not found or skipped on disk:         0
[bytecat]   size changed since the scan             :         0
[bytecat] report written to ~/demo/bytecat-data/reports/audit-dupes-20261030-202030.md
```

## 10. Find a file

```text
$ bytecat ask insurance policy
[bytecat] keyword results only: no model server or model at http://127.0.0.1:9/v1
 1. home-policy-2024.pdf   36.0KB  document  [3 copies]
    ~/demo/laptop/Documents/insurance/home-policy-2024.pdf

$ bytecat ask tax return --host nas
[bytecat] keyword results only: no model server or model at http://127.0.0.1:9/v1
 1. 2023-return.pdf   47.1KB  [3 copies]
    nas:share  backup/laptop/Documents/taxes/2023-return.pdf
```

Without a model server, `ask` matches words in paths, file names and labels. Identical
copies are shown once with a count. The NAS result shows no kind because `extract` has not
described it. For a file on the machine you are typing on, the second
line is its full path; for another machine it is `host:drive` and the path inside the drive.

## Keeping it current

Re-run the same `scan ... | bytecat merge` for a drive whenever it has changed, then
`bytecat pipeline`. Files that are gone are marked missing, not deleted from the catalog;
changed files are hashed again. A drive you do not rescan keeps its last state.

Every command and flag is in [commands.md](commands.md).
