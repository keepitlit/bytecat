#!/usr/bin/env python3
"""Re-derive bytecat's riskiest claims from the disk, under a different digest.

WHY THIS EXISTS. Every verdict bytecat produces -- "all 14,604 files landed",
"the FLAC library on the laptop is a strict subset of the one on the NAS", "these 215 files exist
nowhere else" -- is SQL over a catalog, computed once by one codebase running one
algorithm. Re-running the report agrees with itself. That is not evidence, and
those verdicts are what authorise deleting hundreds of gigabytes.

This module answers the same questions by going back to the disk. Two of the
three independences a separate tool (hashdeep) would have supplied survive being
written in-repo, and they were always the larger share:

  * A DIFFERENT ALGORITHM. SHA-256, from hashlib. It can see what a second
    BLAKE2b run never could: a digest_size slip, quick_hash/full_hash confusion,
    a chunked-read offset bug, a 128-bit collision.
  * A FRESH WALK, READING REAL BYTES. Categorically different from querying the
    catalog, same repo or not. It is the only thing that can find a file the
    catalog never saw, or a file the catalog swears is there and is not.

What is NOT claimed: this is the same author and the same repository. A
conceptual error made in both places -- an identical path-normalisation mistake,
an identical exclusion rule -- survives this check. The report says so.

THE INDEPENDENCE CONTRACT, and it is the entire value of this file:

  * stdlib only.
  * The MEASUREMENT path imports NOTHING from the bytecat package. Not hashing.py,
    not scan.py, not report_dupes.py. `db` is imported for a READ-ONLY handle,
    and solely to read back the claim being checked -- never to compute this
    module's own answer.
  * Its OWN traversal. A walker that consumes bytecat's file list cannot find what
    that list missed, which is half the point.
  * Its OWN read strategy, deliberately not hashing.py's uniform 1 MiB loop. A
    chunking bug that hits both is then a bug in two different loops.
  * The SKIP_* sets below are a DELIBERATE DUPLICATE of scan.py's, not an import.
    They must stay in step or drift reporting fills with noise -- but importing
    them would make an exclusion bug invisible to the very check meant to catch
    it. Duplicated on purpose; see the note on the sets themselves.
  * It never writes to the catalog, and never writes to a scanned tree.

STRUCTURAL LIMIT worth understanding: a single tree's hashes cannot be checked
against the catalog with a different algorithm, because SHA-256 and BLAKE2b are
not comparable. What a different algorithm CAN check are bytecat's RELATIONAL
claims -- set membership and equivalence classes -- and those are exactly the
claims that gate every deletion. Hence the two modes.

Like every other report in bytecat this DELETES NOTHING and has no --apply.
Per-file detail goes to a file under ~/.local/share/bytecat/reports/, never to
stdout, so a personal archive's paths stay out of any transcript.

Usage:
    audit.py coverage --source <path> --target <path>
    audit.py dupes --path <path> [--no-drift]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db  # read-only handle, to read back the claim under test

# --------------------------------------------------------------------------
# read strategy -- deliberately NOT hashing.py's
# --------------------------------------------------------------------------
# hashing.py streams every file in uniform 1 MiB chunks. This reads small files
# in a single call and large ones in 4 MiB chunks, so an offset or
# chunk-boundary bug would have to be made twice, differently, to hide here.

SMALL_FILE = 8 << 20   # 8 MiB: read whole
CHUNK = 4 << 20        # 4 MiB: streaming above that


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        if os.fstat(fh.fileno()).st_size <= SMALL_FILE:
            h.update(fh.read())
        else:
            while True:
                b = fh.read(CHUNK)
                if not b:
                    break
                h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------
# traversal -- deliberately NOT scan.py's
# --------------------------------------------------------------------------
# These sets MIRROR scan.py's SKIP_DIRS / SKIP_FILES / SKIP_EXTS / BUNDLE_SUFFIXES
# and are copied ON PURPOSE rather than imported. If audit excluded a different
# set from the scanner, drift reporting would fill with thousands of false
# "on disk but not in the catalog" entries and become unreadable. But importing
# them would mean an exclusion bug in scan.py is invisible to the one check meant
# to catch a missing file. Two copies that must agree is the lesser evil, and it
# is the same bargain scan.py already strikes with hashing.py.
#
# If you change one, change the other, and say so in the commit.

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".jj",
    "node_modules", "bower_components", "vendor", ".pnpm-store", ".yarn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "venv", ".venv", "env", ".env.d", "site-packages", ".eggs",
    "target", "build", "dist", "out", ".next", ".nuxt", ".svelte-kit",
    ".gradle", ".m2", ".cargo", ".rustup", ".stack-work", "DerivedData",
    "Pods", ".expo", ".dart_tool", ".cocoapods",
    ".cache", "Caches", "CachedData", ".Trash", ".Trashes",
    ".DocumentRevisions-V100", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    "System Volume Information", "$RECYCLE.BIN",
    ".docker", ".colima", ".lmstudio", ".ollama", ".android", ".gem",
}

SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"}

SKIP_EXTS = {
    "pyc", "pyo", "class", "o", "obj", "so", "dylib", "a", "lib",
    "lock", "swp", "swo", "tmp", "temp", "part", "crdownload",
}

BUNDLE_SUFFIXES = (
    ".app", ".framework", ".photoslibrary", ".fcpbundle", ".logicx",
    ".sparsebundle", ".xcodeproj", ".xcworkspace", ".lrlibrary", ".lrcat",
    ".aplibrary", ".imovielibrary", ".tvlibrary", ".musiclibrary",
    ".pkg", ".bundle",
)


def key(relpath: str) -> str:
    """Join key for comparing a walked path with a catalogued one.

    The catalog's rows were written by scan.py running on Linux (vfat, ZFS);
    this walk may be on macOS, which hands back NFD where the catalog holds NFC.
    Comparing raw strings would report every accented filename as both "missing
    from the catalog" and "not on disk" at once.
    """
    return unicodedata.normalize("NFC", relpath)


class Entry:
    __slots__ = ("rel", "abspath", "size", "mtime")

    def __init__(self, rel: str, abspath: str, size: int, mtime: int):
        self.rel, self.abspath, self.size, self.mtime = rel, abspath, size, mtime


def walk(root: str, min_size: int = 1) -> tuple[list[Entry], int, list[str]]:
    """Own traversal. Returns (entries, n_bundles_pruned, errors)."""
    entries: list[Entry] = []
    bundles = 0
    errors: list[str] = []
    root = os.path.abspath(root)

    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = list(os.scandir(cur))
        except OSError as e:
            errors.append(f"{cur}: {e}")
            continue
        for ent in it:
            name = ent.name
            try:
                if ent.is_symlink():
                    continue  # a symlink has no content of its own
                if ent.is_dir(follow_symlinks=False):
                    if name in SKIP_DIRS:
                        continue
                    if name.endswith(BUNDLE_SUFFIXES):
                        # scan.py records a bundle as ONE catalog entry, and a
                        # directory has no bytes to digest. Prune and count, so
                        # the report can say why the numbers differ.
                        bundles += 1
                        continue
                    stack.append(ent.path)
                    continue
                if not ent.is_file(follow_symlinks=False):
                    continue
                if name in SKIP_FILES:
                    continue
                ext = name.rsplit(".", 1)[-1].lower() if "." in name[1:] else ""
                if ext in SKIP_EXTS:
                    continue
                st = ent.stat(follow_symlinks=False)
                if st.st_size < min_size:
                    continue
                entries.append(Entry(key(os.path.relpath(ent.path, root)),
                                     ent.path, st.st_size, int(st.st_mtime)))
            except OSError as e:
                errors.append(f"{ent.path}: {e}")
    return entries, bundles, errors


def _hash_job(job: tuple[str, str]) -> tuple[str, Optional[str], Optional[str]]:
    rel, path = job
    try:
        return (rel, sha256_of(path), None)
    except OSError as e:
        return (rel, None, str(e))


def hash_entries(entries: list[Entry], workers: int, label: str) -> tuple[dict, list]:
    """rel -> sha256, plus a list of unreadable paths."""
    out: dict[str, str] = {}
    errs: list[str] = []
    if not entries:
        return out, errs
    total = sum(e.size for e in entries)
    print(f"[bytecat] audit: reading {len(entries):,} files "
          f"({human(total)}) under {label}", file=sys.stderr, flush=True)
    jobs = [(e.rel, e.abspath) for e in entries]
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for rel, sha, err in pool.map(_hash_job, jobs, chunksize=32):
            done += 1
            if sha is None:
                errs.append(f"{rel}: {err}")
            else:
                out[rel] = sha
            if done % 5000 == 0:
                print(f"[bytecat] audit: {done:,}/{len(jobs):,}",
                      file=sys.stderr, flush=True)
    return out, errs


# --------------------------------------------------------------------------
# the claim under test -- read back from the catalog, never recomputed here
# --------------------------------------------------------------------------

def human(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or u == "TB":
            return f"{x:.1f} {u}" if u != "B" else f"{int(x)} B"
        x /= 1024
    return f"{x:.1f} TB"


class Scope:
    """A path, resolved to the catalog volume and relpath prefix it sits under."""

    def __init__(self, vol_id: int, uuid: str, mount: str, prefix: str, root: str):
        self.vol_id, self.uuid, self.mount = vol_id, uuid, mount
        self.prefix, self.root = prefix, root

    def catalog_rel(self, rel: str) -> str:
        return key(f"{self.prefix}/{rel}" if self.prefix else rel)

    def __str__(self) -> str:
        return f"{self.uuid}:{self.prefix or '.'} ({self.root})"


def resolve_scope(conn, path: str) -> Scope:
    """Longest-mount-prefix match: which volume does this path live on?"""
    root = os.path.abspath(path)
    if not os.path.isdir(root):
        sys.exit(f"[bytecat] not a directory: {root}")
    best = None
    for r in conn.execute("SELECT id, uuid, last_mount FROM volumes"):
        m = r["last_mount"]
        if not m:
            continue
        m = os.path.abspath(m)
        if root == m or root.startswith(m.rstrip("/") + "/"):
            if best is None or len(m) > len(best[2]):
                best = (r["id"], r["uuid"], m)
    if best is None:
        sys.exit(
            f"[bytecat] {root} is not under any catalogued volume mount.\n"
            "  bytecat can only cross-check a claim it has actually made. Scan\n"
            "  this tree first:  python3 -m bytecat scan <path> | python3 -m bytecat merge")
    vol_id, uuid, mount = best
    prefix = "" if root == mount else os.path.relpath(root, mount)
    return Scope(vol_id, uuid, mount, key(prefix), root)


def _prefix_clause(alias: str, prefix: str) -> tuple[str, list]:
    """SQL fragment restricting `alias` to a relpath prefix.

    substr() rather than LIKE so that a % or _ in a real folder name cannot turn
    into a wildcard. An empty prefix yields substr(x,1,0) = '', which is true.
    """
    p = f"{prefix}/" if prefix else ""
    return f"substr({alias}.relpath, 1, ?) = ?", [len(p), p]


def atlas_coverage_claim(conn, src: Scope, tgt: Scope) -> dict[str, dict]:
    """What bytecat asserts about every source file: is its content on the target?

    Mirrors report_coverage.classify()'s bucket semantics exactly -- present /
    missing / unique / unhashed -- but scoped to a SUBTREE. classify() takes whole
    volumes, and asking bytecat a broader question than the one audit answers would
    manufacture disagreements: "bytecat says present" would mean "somewhere on the
    whole target volume" while audit meant "inside this folder".
    """
    src_c, src_p = _prefix_clause("f", src.prefix)
    tgt_c, tgt_p = _prefix_clause("g", tgt.prefix)
    rows = conn.execute(
        f"""SELECT f.relpath, f.size_bytes, f.blob_id, f.t1_hashed,
                   EXISTS (SELECT 1 FROM files g
                            WHERE g.blob_id = f.blob_id
                              AND g.volume_id = ?
                              AND g.state = 'present'
                              AND {tgt_c}) AS on_target
              FROM files f
             WHERE f.volume_id = ? AND f.state = 'present' AND f.is_symlink = 0
               AND {src_c}""",
        [tgt.vol_id, *tgt_p, src.vol_id, *src_p],
    )
    out: dict[str, dict] = {}
    n = len(f"{src.prefix}/") if src.prefix else 0
    for r in rows:
        rel = key(r["relpath"][n:])
        if r["t1_hashed"] == 0:
            bucket = "unhashed"
        elif r["on_target"]:
            bucket = "present"
        elif r["blob_id"] is None:
            bucket = "unique"
        else:
            bucket = "missing"
        out[rel] = {"bucket": bucket, "size": r["size_bytes"],
                    "present": bucket == "present"}
    return out


# --------------------------------------------------------------------------
# mode 1 -- coverage cross-check
# --------------------------------------------------------------------------

def cmd_coverage(args) -> int:
    conn = db.connect(readonly=True)
    src = resolve_scope(conn, args.source)
    tgt = resolve_scope(conn, args.target)
    if (src.vol_id, src.prefix) == (tgt.vol_id, tgt.prefix):
        sys.exit("[bytecat] source and target are the same tree")

    workers = args.workers or min(8, (os.cpu_count() or 4))

    s_entries, s_bundles, s_walk_err = walk(src.root)
    t_entries, t_bundles, t_walk_err = walk(tgt.root)
    s_hash, s_err = hash_entries(s_entries, workers, "source")
    t_hash, t_err = hash_entries(t_entries, workers, "target")

    target_contents = set(t_hash.values())
    claim = atlas_coverage_claim(conn, src, tgt)

    # Cross-tabulate over files BOTH the walk and the catalog know about. The
    # two off-diagonal cells are the entire product of this run.
    cells = {"agree_present": [], "agree_absent": [],
             "atlas_over": [], "atlas_under": []}
    for rel, sha in s_hash.items():
        c = claim.get(rel)
        if c is None:
            continue  # handled as drift below
        audit_present = sha in target_contents
        if audit_present and c["present"]:
            cells["agree_present"].append((rel, c["size"]))
        elif not audit_present and not c["present"]:
            cells["agree_absent"].append((rel, c["size"], c["bucket"]))
        elif c["present"] and not audit_present:
            cells["atlas_over"].append((rel, c["size"]))
        else:
            cells["atlas_under"].append((rel, c["size"], c["bucket"]))

    on_disk_only = sorted(set(s_hash) - set(claim))
    in_catalog_only = sorted(set(claim) - set(s_hash))

    findings = len(cells["atlas_over"]) + len(cells["atlas_under"])
    out = report_path(args.out, "coverage")
    write_coverage_report(out, src, tgt, cells, on_disk_only, in_catalog_only,
                          claim, s_entries, t_entries,
                          s_bundles + t_bundles,
                          s_walk_err + t_walk_err + s_err + t_err)

    print(f"[bytecat] audit coverage  source={src}  target={tgt}")
    print(f"[bytecat]   agree, content present on target : "
          f"{len(cells['agree_present']):>9,}")
    print(f"[bytecat]   agree, content absent from target: "
          f"{len(cells['agree_absent']):>9,}")
    print(f"[bytecat]   DISAGREE, bytecat over-reports     : "
          f"{len(cells['atlas_over']):>9,}   <- bytecat said copied, disk says no")
    print(f"[bytecat]   DISAGREE, bytecat under-reports    : "
          f"{len(cells['atlas_under']):>9,}")
    print(f"[bytecat]   on disk, not in catalog          : {len(on_disk_only):>9,}")
    print(f"[bytecat]   in catalog, not on disk          : {len(in_catalog_only):>9,}")
    if args.emit_manifest:
        base = Path(args.emit_manifest)
        for tag, root, h, ents in (("source", src.root, s_hash, s_entries),
                                   ("target", tgt.root, t_hash, t_entries)):
            mp = base.with_name(f"{base.stem}-{tag}{base.suffix or '.hashdeep'}")
            emit_manifest(mp, root, h, {e.rel: e.size for e in ents})
            print(f"[bytecat] manifest written to {mp}")

    print(f"[bytecat] report written to {out}")
    print("[bytecat] nothing was deleted or moved.")
    return 1 if findings else 0


# --------------------------------------------------------------------------
# mode 2 -- duplicate-grouping cross-check
# --------------------------------------------------------------------------

def cmd_dupes(args) -> int:
    conn = db.connect(readonly=True)
    scope = resolve_scope(conn, args.path)
    workers = args.workers or min(8, (os.cpu_count() or 4))

    clause, params = _prefix_clause("f", scope.prefix)
    rows = conn.execute(
        f"""SELECT f.relpath, f.size_bytes, f.blob_id
              FROM files f
             WHERE f.volume_id = ? AND f.state = 'present'
               AND f.is_symlink = 0 AND f.blob_id IS NOT NULL
               AND {clause}""",
        [scope.vol_id, *params],
    ).fetchall()

    # Group among the files we can actually READ. blobs.n_files counts across the
    # whole catalog, so a fan-out of 2 may mean one file here and one on a drive
    # that is not plugged in -- re-reading a group of one proves nothing.
    groups: dict[int, list[tuple[str, int]]] = defaultdict(list)
    n = len(f"{scope.prefix}/") if scope.prefix else 0
    for r in rows:
        groups[r["blob_id"]].append((key(r["relpath"][n:]), r["size_bytes"]))
    local = {b: m for b, m in groups.items() if len(m) >= args.min_fanout}

    entries = [Entry(rel, os.path.join(scope.root, rel), size, 0)
               for members in local.values() for rel, size in members]
    entries = [e for e in entries if os.path.isfile(e.abspath)]
    vanished = sum(len(m) for m in local.values()) - len(entries)

    sha, read_err = hash_entries(entries, workers, "duplicate groups")

    # A group bytecat says is one content, that SHA-256 splits, is a BLAKE2b-128
    # collision or a bug. Either way it is the finding you want BEFORE an rm -r.
    splits = []
    for blob_id, members in sorted(local.items()):
        seen = {sha[rel] for rel, _ in members if rel in sha}
        if len(seen) > 1:
            splits.append((blob_id, [(rel, sz, sha.get(rel)) for rel, sz in members]))

    # The other direction, free once every group member is hashed: two DIFFERENT
    # blob_ids whose bytes are in fact identical -- a duplicate bytecat missed.
    by_sha: dict[str, set[int]] = defaultdict(set)
    rel_blob = {rel: b for b, members in local.items() for rel, _ in members}
    for rel, s in sha.items():
        by_sha[s].add(rel_blob[rel])
    merges = sorted(s for s, bl in by_sha.items() if len(bl) > 1)

    # Drift: what the catalog and the filesystem disagree about, by stat alone.
    drift_new: list[tuple[str, int]] = []
    drift_gone: list[str] = []
    drift_size: list[tuple[str, int, int]] = []
    walk_err: list[str] = []
    if not args.no_drift:
        disk, _bundles, walk_err = walk(scope.root)
        disk_by_rel = {e.rel: e for e in disk}
        cat = conn.execute(
            f"""SELECT f.relpath, f.size_bytes FROM files f
                 WHERE f.volume_id = ? AND f.state = 'present' AND f.is_symlink = 0
                   AND {clause}""",
            [scope.vol_id, *params],
        ).fetchall()
        cat_by_rel = {key(r["relpath"][n:]): r["size_bytes"] for r in cat}
        for rel, e in disk_by_rel.items():
            if rel not in cat_by_rel:
                drift_new.append((rel, e.size))
            elif cat_by_rel[rel] != e.size:
                drift_size.append((rel, cat_by_rel[rel], e.size))
        drift_gone = sorted(set(cat_by_rel) - set(disk_by_rel))

    findings = len(splits) + len(merges)
    out = report_path(args.out, "dupes")
    write_dupes_report(out, scope, local, splits, merges, sha,
                       drift_new, drift_gone, drift_size,
                       vanished, read_err + walk_err)

    print(f"[bytecat] audit dupes  {scope}")
    print(f"[bytecat]   groups re-read (fan-out >= {args.min_fanout}) : "
          f"{len(local):>9,}  ({len(entries):,} files)")
    print(f"[bytecat]   GROUPS THAT SPLIT under SHA-256   : {len(splits):>9,}"
          "   <- bytecat called these identical")
    print(f"[bytecat]   duplicates bytecat MISSED           : {len(merges):>9,}")
    if not args.no_drift:
        print(f"[bytecat]   on disk, not in catalog          : {len(drift_new):>9,}")
        print(f"[bytecat]   in catalog, not on disk          : {len(drift_gone):>9,}")
        print(f"[bytecat]   size changed since the scan      : {len(drift_size):>9,}")
    if args.emit_manifest:
        mp = Path(args.emit_manifest)
        emit_manifest(mp, scope.root, sha, {e.rel: e.size for e in entries})
        print(f"[bytecat] manifest written to {mp}")

    print(f"[bytecat] report written to {out}")
    print("[bytecat] nothing was deleted or moved.")
    return 1 if findings else 0


# --------------------------------------------------------------------------
# reporting -- per-file detail on disk, aggregates on stdout
# --------------------------------------------------------------------------

PROVES = """## What this run does and does not prove

PROVES: the bytes on disk were read again, right now, and digested with SHA-256
-- a different algorithm from the BLAKE2b-128 the catalog holds -- by a walker
that enumerated the filesystem itself rather than trusting the catalog's list.
A digest_size slip, a quick/full-hash confusion, a chunk-boundary bug or a
128-bit collision in the catalog would surface here.

DOES NOT PROVE: independence of authorship. This module lives in the same
repository, written by the same hand, as the code it checks. A conceptual error
made identically in both places -- the same path-normalisation assumption, the
same exclusion rule -- survives this check. For a genuinely external opinion,
`--emit-manifest` writes these digests in hashdeep's manifest format so
`hashdeep -a -k <manifest>` can be run against the same tree by a tool that
shares no code with bytecat.
"""


def report_path(explicit: str, kind: str) -> Path:
    if explicit:
        return Path(explicit)
    return db.atlas_home() / "reports" / f"audit-{kind}-{datetime.now():%Y%m%d-%H%M}.md"


def _table(fh, header: str, rows, cols: str) -> None:
    fh.write(f"\n### {header} ({len(rows):,})\n\n")
    if not rows:
        fh.write("_none_\n")
        return
    fh.write(f"| {cols} |\n|{'---|' * (cols.count('|') + 1)}\n")
    for r in rows:
        fh.write("| " + " | ".join(str(x) for x in r) + " |\n")


def write_coverage_report(out: Path, src, tgt, cells, on_disk_only,
                          in_catalog_only, claim, s_entries, t_entries,
                          bundles, errors) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        fh.write("# Audit: coverage cross-check\n\n")
        fh.write(f"Source: {src}\nTarget: {tgt}\n")
        fh.write(f"Generated: {datetime.now():%Y-%m-%d %H:%M}\n\n")
        fh.write(f"Walked {len(s_entries):,} source files "
                 f"({human(sum(e.size for e in s_entries))}) and "
                 f"{len(t_entries):,} target files "
                 f"({human(sum(e.size for e in t_entries))}).\n")
        if bundles:
            fh.write(f"{bundles} bundle(s) pruned -- the catalog records each as a "
                     "single entry and a directory has no bytes to digest.\n")
        fh.write("\nNothing was deleted or moved.\n\n")

        fh.write("## Verdict\n\n")
        fh.write("| | bytecat: present | bytecat: missing/unique |\n|---|---:|---:|\n")
        fh.write(f"| **audit: present** | {len(cells['agree_present']):,} "
                 f"| {len(cells['atlas_under']):,} |\n")
        fh.write(f"| **audit: absent** | **{len(cells['atlas_over']):,}** "
                 f"| {len(cells['agree_absent']):,} |\n\n")
        if not cells["atlas_over"] and not cells["atlas_under"]:
            fh.write("Both off-diagonal cells are empty: bytecat's verdict is "
                     "confirmed by a fresh read of the disk under SHA-256.\n")
        else:
            fh.write("**The off-diagonal cells are non-empty. bytecat and the disk "
                     "disagree.** Do not act on the coverage report until this is "
                     "resolved.\n")

        _table(fh, "bytecat over-reports -- catalog says the content is on the "
                   "target, SHA-256 says it is not", cells["atlas_over"],
               "Path | Bytes")
        _table(fh, "bytecat under-reports -- content IS on the target, catalog "
                   "did not say so", cells["atlas_under"], "Path | Bytes | Catalog bucket")
        _table(fh, "On disk but not in the catalog (source) -- the walk found "
                   "files the catalog has never seen",
               [(p,) for p in on_disk_only], "Path")
        _table(fh, "In the catalog but not on disk (source)",
               [(p, claim[p]["bucket"]) for p in in_catalog_only], "Path | Catalog bucket")
        _table(fh, "Content confirmed absent from the target by both",
               cells["agree_absent"], "Path | Bytes | Catalog bucket")
        if errors:
            _table(fh, "Unreadable", [(e,) for e in errors], "Error")
        fh.write("\n" + PROVES)


def write_dupes_report(out: Path, scope, local, splits, merges, sha,
                       drift_new, drift_gone, drift_size, vanished, errors) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        fh.write("# Audit: duplicate-grouping cross-check\n\n")
        fh.write(f"Scope: {scope}\nGenerated: {datetime.now():%Y-%m-%d %H:%M}\n\n")
        fh.write(f"{len(local):,} duplicate groups re-read "
                 f"({sum(len(m) for m in local.values()):,} files). "
                 "Only files sharing a blob_id are read; the rest of the volume "
                 "is walked by stat alone.\n")
        if vanished:
            fh.write(f"{vanished:,} catalogued group member(s) no longer exist "
                     "on disk and could not be re-read.\n")
        fh.write("\nNothing was deleted or moved.\n\n")

        fh.write("## Verdict\n\n")
        if not splits and not merges:
            fh.write(f"SHA-256 partitions these {len(local):,} groups exactly as "
                     "the catalog's blob_ids do. bytecat's identical-content claim "
                     "holds under a second algorithm.\n")
        else:
            fh.write("**SHA-256 disagrees with the catalog's grouping.** Treat "
                     "every duplicate report touching these groups as unsafe.\n")

        fh.write("\n### Groups bytecat calls identical that SHA-256 splits "
                 f"({len(splits):,})\n\n")
        if not splits:
            fh.write("_none_\n")
        for blob_id, members in splits:
            fh.write(f"\n**blob {blob_id}**\n\n| Path | Bytes | SHA-256 |\n|---|---:|---|\n")
            for rel, size, s in members:
                fh.write(f"| {rel} | {size:,} | {(s or 'unread')[:16]} |\n")

        fh.write(f"\n### Identical content bytecat assigned to different blobs "
                 f"({len(merges):,})\n\n")
        if not merges:
            fh.write("_none_\n")
        else:
            fh.write("| SHA-256 | Paths |\n|---|---|\n")
            for s in merges:
                paths = [r for r, v in sha.items() if v == s]
                fh.write(f"| {s[:16]} | {'<br>'.join(paths)} |\n")

        _table(fh, "On disk, not in the catalog", drift_new, "Path | Bytes")
        _table(fh, "In the catalog, not on disk", [(p,) for p in drift_gone], "Path")
        _table(fh, "Size changed since the scan", drift_size,
               "Path | Catalog bytes | Disk bytes")
        if errors:
            _table(fh, "Unreadable", [(e,) for e in errors], "Error")
        fh.write("\n" + PROVES)


def emit_manifest(path: Path, root: str, sha: dict[str, str],
                  sizes: dict[str, int]) -> None:
    """Write the digests in hashdeep's manifest format.

    A FILE FORMAT, not a dependency -- nothing in bytecat reads this back. It
    exists so the one independence this module cannot supply, a different
    author's code, stays one command away: `hashdeep -a -k <manifest> -r <tree>`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fh.write("%%%% HASHDEEP-1.0\n%%%% size,sha256,filename\n")
        fh.write(f"## Invoked from: {root}\n")
        for rel in sorted(sha):
            fh.write(f"{sizes.get(rel, 0)},{sha[rel]},{rel}\n")


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="bytecat audit",
        description="re-read the disk and cross-check a catalog verdict (read-only)")
    sub = ap.add_subparsers(dest="mode", required=True)

    c = sub.add_parser("coverage",
                       help="is every source file's content really on the target?")
    c.add_argument("--source", required=True)
    c.add_argument("--target", required=True)

    d = sub.add_parser("dupes",
                       help="are the files bytecat calls identical really identical?")
    d.add_argument("--path", required=True)
    d.add_argument("--min-fanout", type=int, default=2)
    d.add_argument("--no-drift", action="store_true",
                   help="skip the stat-only walk for catalog/filesystem drift")

    for p in (c, d):
        p.add_argument("--workers", type=int, default=0)
        p.add_argument("--out", default="")
        p.add_argument("--emit-manifest", default="", metavar="PATH",
                       help="also write the SHA-256 digests in hashdeep manifest "
                            "format, so an external tool can check the same tree")

    args = ap.parse_args(argv)
    try:
        return cmd_coverage(args) if args.mode == "coverage" else cmd_dupes(args)
    except db.NoCatalog as e:
        print(f"[bytecat] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
