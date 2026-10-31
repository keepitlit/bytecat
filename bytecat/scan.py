#!/usr/bin/env python3
"""Portable filesystem scanner. Emits NDJSON on stdout.

DELIBERATELY STDLIB-ONLY AND SINGLE-FILE. This gets copied to hosts where we
cannot install anything (a NAS, an old laptop) and run with whatever Python is
already there. Do not add imports beyond the standard library, and do not import
anything else from the `bytecat` package -- `scp scan.py host:` must be sufficient.

Hashing a million files across SMB would take days and saturate the network, so
the scanner runs ON each host and only its NDJSON comes back over the wire.

Read-only: opens nothing, stats everything. It cannot modify the tree it walks.

Usage:
    scan.py /path/to/root [/another/root ...] > host.ndjson
    scan.py --volume-uuid UUID /mnt/user      # force identity for network shares
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid as uuidlib

# Directories never worth cataloguing: build output, package caches, VCS
# internals, and OS scratch. These dominate the file count (one developer's
# home reported ~1.09M files, the large majority of it node_modules) while containing
# essentially nothing a person would search for.
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
    "Library/Caches",
}

# Files that are pure noise.
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"}

SKIP_EXTS = {
    "pyc", "pyo", "class", "o", "obj", "so", "dylib", "a", "lib",
    "lock", "swp", "swo", "tmp", "temp", "part", "crdownload",
}

# Marker file used to give a stable identity to volumes that have no real UUID
# (SMB/NFS shares). Written ONCE, only with --allow-marker, and it is the single
# exception to the scanner being read-only.
MARKER = ".bytecat-volume-id"


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# volume identity
# --------------------------------------------------------------------------

def macos_volume_info(path: str) -> dict:
    """Ask diskutil for a real volume UUID. macOS only; best-effort.

    Parses diskutil's plain-text output rather than -plist on purpose: plistlib
    imports pyexpat, and a Homebrew Python linked against a newer libexpat than
    the system one fails to import it at all. That is exactly the kind of
    breakage that would silently downgrade every volume to a synthetic id, so
    this path avoids XML entirely.
    """
    try:
        out = subprocess.run(
            ["diskutil", "info", path],
            capture_output=True, timeout=15, text=True,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}

    fields: dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip().lower()] = v.strip()

    total = None
    raw_total = fields.get("container total space") or fields.get("volume total space") or ""
    m = raw_total.split("(")
    if len(m) > 1 and "Bytes" in m[1]:
        digits = "".join(ch for ch in m[1] if ch.isdigit())
        total = int(digits) if digits else None

    return {
        "uuid": fields.get("volume uuid") or fields.get("disk / partition uuid") or "",
        "label": fields.get("volume name") or "",
        "fs_type": fields.get("file system personality") or fields.get("type (bundle)") or "",
        "total_bytes": total,
    }


def mount_point(path: str) -> str:
    """Walk up until the device number changes -- that boundary is the mount."""
    mp = os.path.abspath(path)
    try:
        dev = os.stat(mp).st_dev
        while mp != os.path.dirname(mp) and os.stat(os.path.dirname(mp)).st_dev == dev:
            mp = os.path.dirname(mp)
    except OSError:
        pass
    return mp


# Read at call time, so a test can point them at a fake tree.
MOUNTINFO = "/proc/self/mountinfo"
BY_UUID = "/dev/disk/by-uuid"


def _unescape(field: str) -> str:
    """mountinfo writes a space in a path as \\040, and so on."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def linux_volume_info(mp: str) -> dict:
    """The filesystem UUID of the device mounted at `mp`. Linux only; best-effort.

    Finds the mount's source device in /proc/self/mountinfo and matches it
    against the /dev/disk/by-uuid symlinks, by device number or by the path
    the link resolves to (btrfs reports an anonymous device number, so the
    source path is the only match there). A network share, tmpfs or overlay
    has no entry in by-uuid and returns nothing, and the caller falls back.
    """
    try:
        dev = os.stat(mp).st_dev
    except OSError:
        return {}
    want = f"{os.major(dev)}:{os.minor(dev)}"
    best = None
    try:
        with open(MOUNTINFO) as fh:
            for line in fh:
                head, sep, tail = line.partition(" - ")
                f, t = head.split(), tail.split()
                if not sep or len(f) < 5 or len(t) < 2 or f[2] != want:
                    continue
                entry = (t[0], _unescape(t[1]))
                # Bind mounts share a device; the one mounted at mp wins.
                if best is None or _unescape(f[4]) == mp:
                    best = entry
    except OSError:
        return {}
    fs_type, source = best or ("", "")
    try:
        names = sorted(os.listdir(BY_UUID))
    except OSError:
        return {}
    real = os.path.realpath(source) if source.startswith("/") else ""
    for name in names:
        link = os.path.join(BY_UUID, name)
        try:
            if os.stat(link).st_rdev == dev or (real and os.path.realpath(link) == real):
                return {"uuid": name, "fs_type": fs_type}
        except OSError:
            continue
    return {}


def read_marker(mp: str) -> str:
    try:
        with open(os.path.join(mp, MARKER)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def volume_identity(root: str, forced: str = "", allow_marker: bool = False) -> dict:
    """Resolve a stable identity for the volume containing `root`.

    Identity precedence: explicit --volume-uuid, then a real filesystem UUID
    (diskutil on macOS, /dev/disk/by-uuid on Linux), then a marker file at the
    mount point, then a synthetic fallback.

    Mount points are never used as identity -- a drive that remounts at
    "/Volumes/Backup 1" must not read as a second, duplicate volume.

    With --volume-uuid, the ID scan would have found is kept as `fs_uuid`, so
    status can still recognise the drive when it is mounted here. When a
    filesystem UUID is found, `aliases` lists the IDs a scan without one gave
    the drive (marker, synthetic), so merge can carry its history over.
    """
    info: dict = {"uuid": "", "label": "", "fs_type": "", "total_bytes": None}
    mp = mount_point(root)

    # diskutil answers for a mount point, not for an arbitrary directory
    # underneath one -- querying the scan root directly returns nothing useful.
    if platform.system() == "Darwin":
        info.update({k: v for k, v in macos_volume_info(mp).items() if v})
    elif platform.system() == "Linux":
        info.update({k: v for k, v in linux_volume_info(mp).items() if v})

    marker = read_marker(mp)
    # Last resort: stable for this host+mount, but not across remounts
    # at a different path. Prefer --allow-marker for network shares.
    synthetic = f"synthetic:{platform.node()}:{mp}"
    if info["uuid"]:
        info["aliases"] = [a for a in (marker, synthetic) if a]
    elif marker:
        info["uuid"] = marker
    elif allow_marker and not forced:
        marker_path = os.path.join(mp, MARKER)
        new = str(uuidlib.uuid4())
        try:
            with open(marker_path, "w") as fh:
                fh.write(new + "\n")
            info["uuid"] = new
            eprint(f"[bytecat] wrote volume marker {marker_path}")
        except OSError as e:
            eprint(f"[bytecat] could not write {marker_path} ({e}); "
                   f"use --volume-uuid for a stable ID")
    if not info["uuid"]:
        info["uuid"] = synthetic

    if forced:
        # As before: a forced ID takes no label or type from the drive.
        info = {"uuid": forced, "fs_uuid": info["uuid"], "label": "",
                "fs_type": "", "total_bytes": None}

    try:
        st = os.statvfs(root)
        info["total_bytes"] = info.get("total_bytes") or st.f_blocks * st.f_frsize
    except (OSError, AttributeError):
        pass

    return info


# --------------------------------------------------------------------------
# walk
# --------------------------------------------------------------------------

# Directories that are really ONE logical object. Their internals are opaque
# (managed originals with UUID filenames) and must not be reorganised, but the
# object itself is absolutely something you want in the catalog -- a Photos
# library is often the single largest thing a person owns.
BUNDLE_SUFFIXES = (
    ".app", ".framework", ".photoslibrary", ".fcpbundle", ".logicx",
    ".sparsebundle", ".xcodeproj", ".xcworkspace", ".lrlibrary", ".lrcat",
    ".aplibrary", ".imovielibrary", ".tvlibrary", ".musiclibrary",
    ".pkg", ".bundle",
)


def is_bundle(name: str) -> bool:
    return name.endswith(BUNDLE_SUFFIXES)


def should_skip_dir(name: str, full: str, skip_hidden: bool) -> bool:
    if name in SKIP_DIRS:
        return True
    if skip_hidden and name.startswith(".") and name not in (".",):
        return True
    return False


def bundle_summary(path: str) -> tuple[int, int, int]:
    """(total_bytes, n_files, newest_mtime) for a bundle, by stat only.

    Cheap -- readdir already returns the stat data -- and it means the catalog
    can say "Photos Library, 180 GB" instead of pretending it does not exist.
    """
    total = count = newest = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            for ent in os.scandir(cur):
                try:
                    if ent.is_dir(follow_symlinks=False):
                        stack.append(ent.path)
                    elif ent.is_file(follow_symlinks=False):
                        st = ent.stat(follow_symlinks=False)
                        total += st.st_size
                        count += 1
                        newest = max(newest, int(st.st_mtime))
                except OSError:
                    continue
        except OSError:
            continue
    return total, count, newest


def walk(root: str, *, skip_hidden: bool, min_size: int, descend_bundles: bool = False):
    """Yield file records under `root`.

    Uses os.scandir throughout: it reuses the stat data the OS already returned
    from readdir, which matters enormously at millions of entries.
    """
    stack = [root]

    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except (PermissionError, OSError) as e:
            yield {"_error": f"{type(e).__name__}: {e}", "path": cur}
            continue

        for ent in entries:
            try:
                name = ent.name
                if ent.is_dir(follow_symlinks=False):
                    if should_skip_dir(name, ent.path, skip_hidden):
                        continue
                    if is_bundle(name) and not descend_bundles:
                        # Emit the bundle as one entry rather than dropping it.
                        total, count, newest = bundle_summary(ent.path)
                        st = ent.stat(follow_symlinks=False)
                        yield {
                            "relpath": os.path.relpath(ent.path, root),
                            "name": name,
                            "ext": name.rsplit(".", 1)[-1].lower(),
                            "size": total,
                            "mtime": newest or int(st.st_mtime),
                            "inode": st.st_ino,
                            "nlink": 1,
                            "symlink": 0,
                            "bundle_files": count,
                        }
                        continue
                    stack.append(ent.path)
                    continue

                if ent.is_symlink():
                    # Recorded for completeness but never followed or hashed:
                    # following them is how a scanner ends up in a cycle.
                    st = ent.stat(follow_symlinks=False)
                    yield {
                        "relpath": os.path.relpath(ent.path, root),
                        "name": name, "ext": "", "size": st.st_size,
                        "mtime": int(st.st_mtime), "inode": st.st_ino,
                        "nlink": st.st_nlink, "symlink": 1,
                    }
                    continue

                if not ent.is_file(follow_symlinks=False):
                    continue  # fifo, socket, device
                if name in SKIP_FILES:
                    continue

                ext = name.rsplit(".", 1)[-1].lower() if "." in name[1:] else ""
                if ext in SKIP_EXTS:
                    continue

                st = ent.stat(follow_symlinks=False)
                if st.st_size < min_size:
                    continue

                yield {
                    "relpath": os.path.relpath(ent.path, root),
                    "name": name,
                    "ext": ext,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "inode": st.st_ino,
                    "nlink": st.st_nlink,
                    "symlink": 0,
                }
            except (PermissionError, OSError) as e:
                yield {"_error": f"{type(e).__name__}: {e}", "path": ent.path}

# --------------------------------------------------------------------------
# hash-list mode
#
# hash.py --emit-work has always documented that the remote host runs
# `scan.py --hash-list work.json`, but that mode was never implemented, so the
# emitted work list had no consumer. This is it.
#
# The digests MUST stay byte-identical to bytecat/hashing.py -- a cross-host
# comparison is only meaningful if both sides hash the same way. Same
# algorithm, same digest_size, same chunking, same size-prefix on the quick
# hash. Do not "improve" one copy alone.
# --------------------------------------------------------------------------

CHUNK = 1 << 20          # 1 MiB
QUICK_EDGE = 1 << 20     # head and tail size for the cheap prefilter
DIGEST_SIZE = 16         # 128-bit; ample for identity, half the storage


def quick_hash(path: str, size: int) -> str:
    """Head + tail (1 MiB each) plus the size. Mirrors hashing.quick_hash."""
    h = hashlib.blake2b(digest_size=DIGEST_SIZE)
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(QUICK_EDGE))
        if size > QUICK_EDGE * 2:
            fh.seek(-QUICK_EDGE, 2)
            h.update(fh.read(QUICK_EDGE))
    return h.hexdigest()


def full_hash(path: str) -> str:
    """Mirrors hashing.full_hash."""
    h = hashlib.blake2b(digest_size=DIGEST_SIZE)
    with open(path, "rb", buffering=0) as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def run_hash_list(path: str, root_override: str = "",
                  progress_every: int = 500) -> int:
    """Hash the files named in a work list from `hash.py --emit-work`.

    Reads {"jobs": [{"id", "root", "relpath", "size"}]} and emits one NDJSON
    record per file. Read-only: opens 'rb' and writes nothing anywhere.

    A job's `root` is the volume's last_mount on the machine that catalogued
    it, which is not necessarily where it is mounted here (a container bind
    mount, say) -- `--hash-root` overrides it.
    """
    with open(path) as fh:
        work = json.load(fh)
    jobs = work.get("jobs", [])
    done = n_err = 0
    t0 = time.time()
    for job in jobs:
        root = root_override or job.get("root") or ""
        fpath = os.path.join(root, job["relpath"])
        try:
            size = os.path.getsize(fpath)
            rec = {"type": "hash", "id": job["id"], "size": size,
                   "quick": quick_hash(fpath, size), "full": full_hash(fpath)}
        except OSError as e:
            # Report it rather than dropping it: a file we could not read must
            # never be silently mistaken for one that is absent.
            n_err += 1
            rec = {"type": "hash_error", "id": job["id"],
                   "relpath": job["relpath"], "error": e.__class__.__name__}
        print(json.dumps(rec), flush=True)
        done += 1
        if progress_every and done % progress_every == 0:
            eprint(f"[bytecat] hashed {done:,}/{len(jobs):,}  {time.time()-t0:.0f}s")
    # done counts unreadable files too; report the ones actually hashed.
    eprint(f"[bytecat] hashed {done - n_err:,} files; {n_err:,} unreadable")
    return 0


def main() -> int:
    # Standalone on a NAS host (python3 scan.py) it keeps its own name.
    ap = argparse.ArgumentParser(prog="bytecat scan" if __package__ else None,
        description="list files under each root as NDJSON for bytecat merge")
    ap.add_argument("roots", nargs="*", help="folders to list")
    # BYTECAT_HOST, as db.this_host() reads it, so the tiers find this drive.
    ap.add_argument("--host",
                    default=os.environ.get("BYTECAT_HOST") or platform.node().split(".")[0],
                    help="host name to record (default: this machine's name)")
    ap.add_argument("--volume-uuid", default="",
                    help="set the volume ID (for network shares)")
    ap.add_argument("--allow-marker", action="store_true",
                    help=f"write {MARKER} at the volume root if it has no ID")
    ap.add_argument("--include-hidden", action="store_true",
                    help="also descend into folders whose names start with .")
    ap.add_argument("--descend-bundles", action="store_true",
                    help="list the files inside bundles (.photoslibrary, .app, ...)")
    ap.add_argument("--min-size", type=int, default=1,
                    help="skip files smaller than this many bytes (default: skip empty)")
    ap.add_argument("--progress-every", type=int, default=20000,
                    help="progress line every N files (default 20000; 0 for none)")
    ap.add_argument("--hash-list", default="",
                    help="hash files listed by `bytecat hash --emit-work`; "
                         "return the output to --ingest")
    ap.add_argument("--hash-root", default="",
                    help="read the listed files under this directory "
                         "instead of their catalogued root")
    args = ap.parse_args()

    if args.hash_list:
        return run_hash_list(args.hash_list, args.hash_root)

    if not args.roots:
        ap.error("give at least one root, or use --hash-list")

    n_files = n_err = 0
    t0 = time.time()

    for root in args.roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            eprint(f"[bytecat] not a directory, skipping: {root}")
            continue

        vol = volume_identity(root, args.volume_uuid, args.allow_marker)
        print(json.dumps({
            "type": "volume",
            "host": args.host,
            "platform": platform.system().lower(),
            "root": root,
            # Lets merge place two roots on one drive, and follow a remount.
            "mount": mount_point(root),
            "scan_started": t0,
            **vol,
        }), flush=True)

        for rec in walk(root, skip_hidden=not args.include_hidden,
                        min_size=args.min_size,
                        descend_bundles=args.descend_bundles):
            if "_error" in rec:
                n_err += 1
                # Path only -- never content.
                print(json.dumps({"type": "error", "uuid": vol["uuid"], **rec}))
                continue
            rec["type"] = "file"
            rec["uuid"] = vol["uuid"]
            print(json.dumps(rec))
            n_files += 1
            if args.progress_every and n_files % args.progress_every == 0:
                eprint(f"[bytecat] {n_files:,} files  {time.time()-t0:.0f}s")

    print(json.dumps({
        "type": "summary", "host": args.host, "n_files": n_files,
        "n_errors": n_err, "elapsed": round(time.time() - t0, 1),
    }), flush=True)
    # n_err counts unreadable folders as one each, so they are paths, not files.
    eprint(f"[bytecat] scanned {n_files:,} files; {n_err:,} paths unreadable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
