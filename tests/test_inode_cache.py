#!/usr/bin/env python3
"""Prove the inode cache survives a rename, and that it changes nothing else.

The catalog is keyed on (volume_id, relpath), so before this existed a moved
file arrived as a new row with blob_id NULL and was read from disk again --
even though its inode, mtime and size never changed, and even though the old
path's row still held the answer.

A cache only ever observed on a tree where it makes no difference has proved
nothing. So this renames files that MUST be re-read without it, and asserts the
two modes disagree about the work done while agreeing about the answer.

It also pins the hazard: a file whose bytes change while its mtime does not gets
the WRONG blob. That is true with the inode cache and equally true without it --
the relpath cache in db.upsert_files has always had it -- and the assertion below
is what stops that being quietly attributed to this change.
"""
import os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def seed(root):
    """2 identical files (they get a real blob) + 1 size-unique orphan."""
    root.mkdir(parents=True)
    body = os.urandom(3000)
    (root / "dupA.bin").write_bytes(body)
    (root / "dupB.bin").write_bytes(body)
    (root / "uniq.bin").write_bytes(os.urandom(5077))


def env_for(home):
    return dict(os.environ, BYTECAT_HOME=str(home), XDG_DATA_HOME=str(home))


def cycle(home, src, *hash_args):
    """scan -> merge -> hash. Returns (rows, hash stderr, returncode, counts).

    counts are the hash summary's numbers in order: hashed, reused (inherited
    by inode), need no read, unreadable. Read by position, so the checks hold
    the counts and not the sentence."""
    env = env_for(home)
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(src), "--host", "h",
                        "--volume-uuid", "u-1"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)
    h = subprocess.run([sys.executable, "-m", "bytecat.hash", *hash_args],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    return dump(home), h.stderr, h.returncode, summary_counts(h.stdout)


def summary_counts(out):
    for line in out.splitlines():
        if line.startswith("[bytecat] hashed"):
            return [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", line)]
    return [None] * 4


def dump(home):
    """relpath -> (t1_hashed, blob_id-or-None) for PRESENT rows."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,'.');from bytecat import db;"
         "c=db.open_catalog();"
         "print('\\n'.join(f\"{r['relpath']}|{r['t1_hashed']}|{r['blob_id']}\""
         " for r in c.execute(\"SELECT relpath,t1_hashed,blob_id FROM files\""
         " \" WHERE state='present' ORDER BY relpath\")))"],
        cwd=ROOT, env=env_for(home), capture_output=True, text=True)
    rows = {}
    for line in out.stdout.strip().splitlines():
        p, t1, blob = line.split("|")
        rows[p] = (t1 == "1", None if blob == "None" else int(blob))
    return rows


tmp = Path(tempfile.mkdtemp(prefix="bytecat-inode-"))
try:
    # ---------------------------------------------------------------- baseline
    home = tmp / "home"
    src = tmp / "src"
    seed(src)
    base, base_err, rc, _ = cycle(home, src)
    check("baseline run", rc == 0, base_err[-300:])
    check("identical files share one blob",
          base["dupA.bin"][1] is not None and base["dupA.bin"][1] == base["dupB.bin"][1],
          f"{base}")
    check("size-unique file closed out with NO blob", base["uniq.bin"] == (True, None),
          f"{base.get('uniq.bin')}")
    original_blob = base["dupA.bin"][1]

    # -------------------------------------------------- rename + hardlink pass
    # A rename preserves inode and mtime; only the directory entry moves.
    (src / "sub").mkdir()
    os.rename(src / "dupA.bin", src / "sub" / "dupA.bin")
    os.rename(src / "uniq.bin", src / "sub" / "uniq.bin")
    os.link(src / "dupB.bin", src / "hard.bin")

    moved, moved_err, rc, moved_n = cycle(home, src)
    check("rename run", rc == 0, moved_err[-300:])

    check("moved file KEPT its blob without being read",
          moved["sub/dupA.bin"][1] == original_blob,
          f"{moved.get('sub/dupA.bin')} vs {original_blob}")
    check("hardlink adopted the blob too",
          moved["hard.bin"][1] == original_blob, f"{moved.get('hard.bin')}")
    check("nothing was read from disk", moved_n[0] == 0, f"{moved_n}")

    # Exactly two adoptions: the moved duplicate and the hardlink. The moved
    # size-unique file must NOT be adopted -- its donor row carries
    # (blob_id NULL, t1_hashed 1), which is a claim about the catalog's current
    # population, not a fingerprint.
    check("adoption count is 2, not 3 -- the size-unique donor was refused",
          moved_n[1] == 2, f"{moved_n}")
    check("moved size-unique file still has no blob",
          moved["sub/uniq.bin"] == (True, None), f"{moved.get('sub/uniq.bin')}")

    # ------------------------------------------------- the same tree, no cache
    home2 = tmp / "home2"
    src2 = tmp / "src2"
    seed(src2)
    cycle(home2, src2, "--no-inode-cache")
    (src2 / "sub").mkdir()
    os.rename(src2 / "dupA.bin", src2 / "sub" / "dupA.bin")
    os.rename(src2 / "uniq.bin", src2 / "sub" / "uniq.bin")
    os.link(src2 / "dupB.bin", src2 / "hard.bin")
    nocache, nocache_err, rc, nocache_n = cycle(home2, src2, "--no-inode-cache")

    check("--no-inode-cache run", rc == 0, nocache_err[-300:])
    check("the two modes genuinely disagree about the WORK",
          (nocache_n[0] or 0) > 0 and moved_n[0] == 0,
          f"nocache={nocache_n} cache={moved_n}")
    check("...and agree about the ANSWER",
          (nocache["sub/dupA.bin"][1] == nocache["dupB.bin"][1] == nocache["hard.bin"][1])
          and (moved["sub/dupA.bin"][1] == moved["dupB.bin"][1] == moved["hard.bin"][1]),
          f"nocache={nocache} cache={moved}")
    check("--no-inode-cache reports no adoptions", nocache_n[1] == 0, f"{nocache_n}")

    # ------------------------------------------- the stale-mtime hazard, pinned
    # Rewrite the bytes at the SAME size and put the mtime back. This is what
    # rsync -t, tar extraction and touch -r do. Both cache paths get it wrong,
    # which is the point: the exposure predates the inode key.
    def stale_rewrite(tree):
        target = tree / "dupB.bin"
        st = os.stat(target)
        target.write_bytes(os.urandom(3000))          # same size, different bytes
        os.utime(target, (st.st_atime, st.st_mtime))  # ...and the same mtime

    stale_rewrite(src)
    stale, _, rc, _ = cycle(home, src)
    check("stale-mtime file still claims the OLD blob (known hazard)",
          stale["dupB.bin"][1] == original_blob, f"{stale.get('dupB.bin')}")

    stale_rewrite(src2)
    stale2, _, rc, _ = cycle(home2, src2, "--no-inode-cache")
    check("--no-inode-cache is wrong in EXACTLY the same way "
          "(the hazard is the relpath cache, not this change)",
          stale2["dupB.bin"][1] == nocache["dupB.bin"][1],
          f"{stale2.get('dupB.bin')} vs {nocache.get('dupB.bin')}")

    # --forget is the escape hatch, and it must actually fix it.
    forgot, forgot_err, rc, _ = cycle(home, src, "--forget", "--volume", "u-1")
    check("--forget run", rc == 0, forgot_err[-300:])
    check("--forget re-read the bytes and split the blob",
          forgot["dupB.bin"][1] != forgot["sub/dupA.bin"][1],
          f"dupB={forgot.get('dupB.bin')} dupA={forgot.get('sub/dupA.bin')}")
    # It clears every row on the volume, the 2 missing donors included: 4 + 2.
    check("--forget reports the 6 rows it cleared",
          any(re.findall(r"\d+", line) == ["6"] for line in forgot_err.splitlines()),
          forgot_err[-200:])

    # --forget without --volume is refused: it would re-read every mounted volume.
    r = subprocess.run([sys.executable, "-m", "bytecat.hash", "--forget"],
                       cwd=ROOT, env=env_for(home), capture_output=True, text=True)
    check("--forget without --volume is refused", r.returncode == 2, r.stderr[-200:])

    # -------------------------------------------------------- nothing was moved
    check("source tree still holds every file",
          (src / "sub" / "dupA.bin").exists() and (src / "dupB.bin").exists()
          and (src / "sub" / "uniq.bin").exists() and (src / "hard.bin").exists())
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
