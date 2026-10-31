#!/usr/bin/env python3
"""Tier 1: two-stage content hashing. Resumable, parallel, read-only.

The whole point is to read as few bytes as possible:

  Stage 0  size grouping   -- free, from the catalog. A file whose size is
                              unique on record CANNOT have a duplicate, so it
                              is marked done and never opened. This eliminates
                              the large majority of files.
  Stage 1  quick hash      -- head+tail 1 MiB, only for size-collision candidates.
  Stage 2  full hash       -- only within (size, quick_hash) groups of 2+.

A file left with blob_id IS NULL and t1_hashed = 1 is *provably unique by size*.

Usage:
    hash.py                      # hash everything reachable on this machine
    hash.py --volume TESTVOL
    hash.py --emit-work nas > work.json   # for a host we cannot reach directly
    hash.py --ingest results.ndjson
    hash.py --hash-all           # read every file; fingerprint for all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import Iterator, Optional

from . import db
from .hashing import full_hash, quick_hash

BATCH = 2000


# --------------------------------------------------------------------------
# worker (runs in a subprocess; must not touch the DB)
# --------------------------------------------------------------------------

def _work(job: tuple[int, str, int, bool]) -> tuple[int, Optional[str], Optional[str]]:
    """Return (file_id, quick, full). Either hash may be None on error."""
    file_id, path, size, want_full = job
    # Bundles are recorded as one catalog entry but are directories on disk, and
    # a file can vanish between the scan and this pass. Neither is an error.
    if not os.path.isfile(path):
        return (file_id, None, None)
    try:
        q = quick_hash(path, size)
        f = full_hash(path) if want_full else None
        return (file_id, q, f)
    except (OSError, PermissionError):
        return (file_id, None, None)


# --------------------------------------------------------------------------
# path resolution
# --------------------------------------------------------------------------

def local_volumes(conn, only: Optional[list[int]] = None) -> dict[int, str]:
    """This host's volumes that are mounted here right now.

    A volume that is offline (unplugged, or belongs to another host) is skipped
    silently -- that is the normal case, not an error. Another host's drive is
    skipped even when its scan path exists here: those files are not its bytes,
    and --emit-work is how it gets hashed.
    """
    out: dict[int, str] = {}
    here = db.this_host()
    states = db.volume_states(conn)
    q = """SELECT v.id, v.uuid, v.last_mount, h.name AS host
           FROM volumes v JOIN hosts h ON h.id = v.host_id"""
    for r in conn.execute(q):
        if only and r["id"] not in only:
            continue
        if r["host"] == here and states.get(r["id"]) == "online":
            out[r["id"]] = r["last_mount"]
    return out


def build_dup_sizes(conn, all_sizes: bool = False) -> int:
    """Materialise the set of sizes shared by 2+ files, across the WHOLE catalog.

    Catalog-wide rather than per-volume on purpose: the cross-host duplicate
    ("this photo is on the Mac AND the NAS") is the single most valuable thing
    this report can surface, and a per-volume grouping would never see it.

    With all_sizes (the --hash-all path) the HAVING is dropped so the table
    holds EVERY size. That is the single switch the whole flag needs: the
    `size_bytes IN dup_sizes` filter in candidates() and emit_work() then
    matches everything, and mark_unique_by_size degenerates to closing out
    symlinks and zero-byte files -- which is still right, since a symlink has
    no content of its own and every empty file shares one hash. Size-uniqueness
    is only a property of the catalog at this instant, so a file it eliminates
    today may collide once another drive is added; --hash-all buys a fingerprint
    that stays true.
    """
    having = "" if all_sizes else " HAVING COUNT(*) > 1"
    conn.execute("DROP TABLE IF EXISTS temp.dup_sizes")
    conn.execute(f"""
        CREATE TEMP TABLE dup_sizes AS
        SELECT size_bytes FROM files
        WHERE state = 'present' AND is_symlink = 0 AND size_bytes > 0
        GROUP BY size_bytes{having}
    """)
    conn.execute("CREATE INDEX temp.idx_dup_sizes ON dup_sizes(size_bytes)")
    return conn.execute("SELECT COUNT(*) FROM dup_sizes").fetchone()[0]


def candidates(conn, volume_ids: list[int]) -> Iterator[tuple[int, int, int, str]]:
    """Yield (file_id, volume_id, size, relpath) for possible duplicates.

    relpath comes back in the same query rather than a lookup per file -- at
    catalog scale an N+1 here would dominate the whole stage.

    Hardlinks are excluded later: same volume + same inode is one physical file
    wearing two names, not wasted space.
    """
    placeholders = ",".join("?" * len(volume_ids))
    yield from (
        (r["id"], r["volume_id"], r["size_bytes"], r["relpath"])
        for r in conn.execute(
            f"""SELECT f.id, f.volume_id, f.size_bytes, f.relpath
                FROM files f
                WHERE f.t1_hashed = 0 AND f.state = 'present'
                  AND f.is_symlink = 0 AND f.size_bytes > 0
                  AND f.volume_id IN ({placeholders})
                  AND f.size_bytes IN (SELECT size_bytes FROM dup_sizes)
                ORDER BY f.size_bytes""",
            volume_ids,
        )
    )


def mark_unique_by_size(conn, volume_ids: list[int]) -> int:
    """Close out every file that cannot possibly be a duplicate. Reads nothing."""
    placeholders = ",".join("?" * len(volume_ids))
    cur = conn.execute(
        f"""UPDATE files SET t1_hashed = 1
            WHERE t1_hashed = 0 AND state = 'present'
              AND volume_id IN ({placeholders})
              AND (
                  is_symlink = 1 OR size_bytes = 0
                  OR size_bytes NOT IN (SELECT size_bytes FROM dup_sizes)
              )""",
        volume_ids,
    )
    return cur.rowcount


def adopt_by_inode(conn, volume_ids: list[int]) -> int:
    """Inherit a fingerprint from a row that describes the SAME file elsewhere.

    `files` is keyed on (volume_id, relpath), so db.upsert_files can only keep a
    blob_id when a path stays put. Rename or move a file and it arrives as a new
    row with blob_id NULL -- and gets read from disk again -- even though its
    inode, mtime and size never changed. Meanwhile the old path's row is marked
    'missing' but KEEPS its blob_id, so the answer was already in the catalog and
    simply never consulted. This is the whole fix: one UPDATE, no bytes read.

    Three deliberate restrictions:

      donor blob_id IS NOT NULL -- a row closed out by mark_unique_by_size carries
        (blob_id NULL, t1_hashed 1), which is a claim about the catalog's current
        population, NOT a fingerprint. Inheriting it would propagate a proof that
        may already be stale. Those rows fall back into stage 0, which costs
        nothing because it reads no bytes.
      donor state is NOT filtered -- a 'missing' donor is a rename, a 'present'
        donor is a hardlink. Both are correct and one query covers both.
      same volume only -- an inode means nothing across filesystems.

    THE HAZARD, stated plainly: mtime can survive a content change (rsync -t, tar
    extract, touch -r, some editors), and such a file inherits the wrong blob.
    That exposure is NOT new -- the existing relpath cache in db.upsert_files has
    had exactly it since the catalog was built. This widens the set of rows the
    trust can reach, not the nature of the trust. `--forget` is the escape hatch,
    and tests/test_inode_cache.py asserts the behaviour is identical with and
    without this function, so the hazard stays documented rather than inherited.
    """
    if not volume_ids:
        return 0
    ph = ",".join("?" * len(volume_ids))
    donor = """SELECT g.blob_id FROM files g
                 WHERE g.volume_id  = f.volume_id
                   AND g.inode      = f.inode
                   AND g.mtime      = f.mtime
                   AND g.size_bytes = f.size_bytes
                   AND g.blob_id IS NOT NULL
                   AND g.id != f.id
                 LIMIT 1"""
    cur = conn.execute(
        f"""UPDATE files AS f
               SET blob_id = ({donor}), t1_hashed = 1
             WHERE f.t1_hashed = 0 AND f.state = 'present'
               AND f.is_symlink = 0 AND f.inode > 0 AND f.size_bytes > 0
               AND f.volume_id IN ({ph})
               AND EXISTS ({donor})""",
        volume_ids,
    )
    return cur.rowcount


def forget_volume(conn, volume_id: int) -> int:
    """Drop every tier-1 fingerprint on one volume so it is measured again.

    bytecat otherwise has NO way to force a re-read: --hash-all only disables the
    stage-0 size elimination and still honours t1_hashed = 1. This is the answer
    to a file whose bytes changed while its mtime did not -- the one case both
    the relpath cache and adopt_by_inode get wrong.

    Missing rows are cleared too, not just present ones. They are exactly the
    donors adopt_by_inode would inherit from, so leaving them would let the
    suspect fingerprint walk straight back in on the next pass.
    """
    cur = conn.execute(
        "UPDATE files SET blob_id = NULL, t1_hashed = 0 WHERE volume_id = ?",
        (volume_id,),
    )
    return cur.rowcount


# --------------------------------------------------------------------------
# blob assignment
# --------------------------------------------------------------------------

def assign_blobs(conn, results: list[tuple[int, str, str, int]]) -> None:
    """Attach files to blobs keyed by full hash.

    `results` is (file_id, quick, full, size). Rows whose full hash is None got
    a quick hash only -- they stay t1_hashed = 0 so stage 2 can pick them up.
    """
    for file_id, q, full, size in results:
        if full is None:
            # Quick hash only (a remote host that reported stage 1 alone).
            # Leave t1_hashed = 0 so stage 2 picks it up; creating a hash-less
            # blob row here would just litter the table.
            continue
        row = conn.execute("SELECT id FROM blobs WHERE hash = ?", (full,)).fetchone()
        if row:
            blob_id = row["id"]
        else:
            blob_id = conn.execute(
                "INSERT INTO blobs (hash, size_bytes, quick_hash) VALUES (?,?,?)",
                (full, size, q),
            ).lastrowid
        conn.execute(
            "UPDATE files SET blob_id = ?, t1_hashed = 1 WHERE id = ?", (blob_id, file_id)
        )


def refresh_fanout(conn) -> None:
    """Recompute blobs.n_files -- how many distinct paths share this content."""
    conn.execute("""
        UPDATE blobs SET n_files = (
            SELECT COUNT(*) FROM files
            WHERE files.blob_id = blobs.id AND files.state = 'present'
        )
    """)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def run(conn, *, only_volume: Optional[list[int]] = None, workers: int = 0, limit: int = 0,
        hash_all: bool = False, inode_cache: bool = True,
        forget: bool = False) -> dict:
    vols = local_volumes(conn, only_volume)
    if not vols:
        if not conn.execute("SELECT 1 FROM volumes LIMIT 1").fetchone():
            print("[bytecat] catalog is empty; run bytecat scan ROOT | bytecat merge first",
                  file=sys.stderr)
        elif only_volume:
            print("[bytecat] --volume is not mounted here; "
                  "remote hosts: --emit-work HOST", file=sys.stderr)
        else:
            print("[bytecat] no catalogued volume is mounted here; "
                  "remote hosts: --emit-work HOST", file=sys.stderr)
        return {}

    workers = workers or min(8, (os.cpu_count() or 4))
    vol_ids = list(vols)
    stats = {"skipped_unique": 0, "adopted": 0, "quick": 0, "full": 0, "errors": 0}
    run_id = db.start_run(conn, "hash")

    if forget:
        n = sum(forget_volume(conn, v) for v in vol_ids)
        # Everything on the volume is measured again below.
        print(f"[bytecat] cleared fingerprints on {n:,} files", file=sys.stderr)

    # Stage -1 -- inherit fingerprints across renames, moves and hardlinks.
    # Runs BEFORE stage 0 so adopted rows drop out of the candidate set for free.
    # The count is reported once, as "reused", in the summary.
    if inode_cache:
        stats["adopted"] = adopt_by_inode(conn, vol_ids)

    # Stage 0 -- free elimination. Under --hash-all only symlinks and empty
    # files are closed out; the summary reports them as "need no read".
    build_dup_sizes(conn, all_sizes=hash_all)
    stats["skipped_unique"] = mark_unique_by_size(conn, vol_ids)

    # Stage 1+2. Files sharing a size are hashed quick AND full in one pass:
    # for a size-collision group we are almost certainly going to need the full
    # hash anyway, and one open beats two.
    jobs: list[tuple[int, str, int, bool]] = []
    sizes: dict[int, int] = {}
    for file_id, vol_id, size, relpath in candidates(conn, vol_ids):
        jobs.append((file_id, os.path.join(vols[vol_id], relpath), size, True))
        sizes[file_id] = size
        if limit and len(jobs) >= limit:
            break

    if not jobs:
        # The summary line that follows already says 0 hashed.
        db.finish_run(conn, run_id, status="ok", n_seen=stats["skipped_unique"])
        refresh_fanout(conn)
        return stats

    print(f"[bytecat] hashing {len(jobs):,} files", file=sys.stderr)

    done = 0
    pending: list[tuple[int, str, str, int]] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for file_id, q, full in pool.map(_work, jobs, chunksize=64):
            done += 1
            if q is None:
                stats["errors"] += 1
                # Do not retry forever on a permission error.
                conn.execute("UPDATE files SET t1_hashed = 1 WHERE id = ?", (file_id,))
                continue
            stats["quick"] += 1
            if full:
                stats["full"] += 1
            pending.append((file_id, q, full, sizes[file_id]))

            if len(pending) >= BATCH:
                with db.transaction(conn):
                    assign_blobs(conn, pending)
                pending.clear()
                print(f"[bytecat] {done:,}/{len(jobs):,}", file=sys.stderr)

    if pending:
        with db.transaction(conn):
            assign_blobs(conn, pending)

    refresh_fanout(conn)
    db.finish_run(conn, run_id, status="ok", n_seen=len(jobs),
                  n_changed=stats["full"], n_errors=stats["errors"])
    return stats


def emit_work(conn, host: str, *, hash_all: bool = False,
              only_volume: Optional[list[int]] = None) -> None:
    """Emit a hashing work list for a host we cannot mount locally.

    The host runs `scan.py --hash-list work.json` (stdlib-only) and returns
    NDJSON, which `--ingest` absorbs. Demand-driven, so a remote host only ever
    reads the files that are actually duplicate candidates.
    """
    build_dup_sizes(conn, all_sizes=hash_all)
    # is_symlink/size guards mirror candidates(): under --hash-all the dup_sizes
    # filter no longer excludes them, and neither is worth shipping to a host.
    only_volume = only_volume or []
    extra = (f" AND v.id IN ({','.join('?' * len(only_volume))})"
             if only_volume else "")
    rows = conn.execute(
        f"""SELECT f.id, f.relpath, f.size_bytes, v.last_mount, v.uuid
           FROM files f
           JOIN volumes v ON v.id = f.volume_id
           JOIN hosts   h ON h.id = v.host_id
           WHERE h.name = ? AND f.t1_hashed = 0 AND f.state = 'present'
             AND f.is_symlink = 0 AND f.size_bytes > 0
             AND f.size_bytes IN (SELECT size_bytes FROM dup_sizes){extra}""",
        (host, *only_volume),
    ).fetchall()
    json.dump(
        {"host": host,
         "jobs": [{"id": r["id"], "root": r["last_mount"],
                   "relpath": r["relpath"], "size": r["size_bytes"]} for r in rows]},
        sys.stdout,
    )
    if conn.execute("SELECT 1 FROM hosts WHERE name = ?", (host,)).fetchone():
        print(f"[bytecat] {len(rows):,} files to hash on {host}", file=sys.stderr)
    else:
        print(f"[bytecat] no host named {host}", file=sys.stderr)


def ingest(conn, path: str) -> tuple[int, int]:
    """Absorb NDJSON hash results produced by a remote host.

    Returns (hashes ingested, hash_error records seen); the errors are files
    the host could not read, which stay unhashed here.
    """
    pending = []
    n_err = 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("type") == "hash_error":
                n_err += 1
                continue
            if r.get("type") != "hash":
                continue
            pending.append((r["id"], r.get("quick"), r.get("full"), r["size"]))
    with db.transaction(conn):
        assign_blobs(conn, pending)
    refresh_fanout(conn)
    return len(pending), n_err


def main() -> int:
    ap = argparse.ArgumentParser(prog="bytecat hash",
        description="hash file contents to find duplicates (tier 1)")
    ap.add_argument("--volume", default="", help="restrict to one drive: its UUID, label or mount")
    ap.add_argument("--workers", type=int, default=0,
                    help="parallel readers (default: CPU count, at most 8)")
    ap.add_argument("--limit", type=int, default=0, help="hash at most N files")
    ap.add_argument("--emit-work", metavar="HOST", default="",
                    help="print the JSON work list HOST must hash, instead of hashing")
    ap.add_argument("--hash-all", action="store_true",
                    help="also hash unique-size files; "
                         "with --forget, already-skipped ones too")
    ap.add_argument("--no-inode-cache", action="store_true",
                    help="do not reuse fingerprints for moved, renamed "
                         "or hard-linked files")
    ap.add_argument("--forget", action="store_true",
                    help="clear --volume's fingerprints and hash it again")
    ap.add_argument("--ingest", metavar="NDJSON", default="",
                    help="load results of scan.py --hash-list from another host")
    args = ap.parse_args()

    conn = db.open_catalog()
    only = db.resolve_volume(conn, args.volume) if args.volume else None

    if args.emit_work:
        emit_work(conn, args.emit_work, hash_all=args.hash_all,
                  only_volume=only)
        return 0
    if args.ingest:
        n, n_err = ingest(conn, args.ingest)
        unreadable = f"; {n_err:,} unreadable on the host" if n_err else ""
        print(f"[bytecat] ingested {n:,} hashes{unreadable}")
        return 0

    if args.forget and not args.volume:
        # Forgetting every mounted volume at once is almost never what someone
        # means, and it is expensive to undo -- it re-reads the array.
        print("[bytecat] --forget requires --volume", file=sys.stderr)
        return 2

    stats = run(conn, only_volume=only, workers=args.workers,
                limit=args.limit, hash_all=args.hash_all,
                inode_cache=not args.no_inode_cache, forget=args.forget)
    if stats:
        # reused = fingerprints inherited by inode (moves, renames, hard links);
        # need no read = unique sizes, symlinks and empty files.
        print(f"[bytecat] hashed {stats['full']:,}; "
              f"reused {stats['adopted']:,}; "
              f"{stats['skipped_unique']:,} need no read; "
              f"{stats['errors']:,} unreadable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
