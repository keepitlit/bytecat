#!/usr/bin/env python3
"""Prove `bytecat audit` can actually disagree with bytecat.

An audit that always agrees is decoration. So this does two things that most
tests do not: it seeds a target tree that is a DELIBERATELY imperfect copy (one
file absent, one re-filed under a new name, one same-size-but-corrupted), and
then it LIES to the catalog -- writing claims that are false about the disk --
and asserts the audit catches every one and exits non-zero.

It also pins the independence contract: a file dropped on disk that the catalog
has never seen must show up. If audit ever started consuming bytecat's file list
instead of walking, that check goes quiet and this fails.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def env_for(home):
    return dict(os.environ, BYTECAT_HOME=str(home), XDG_DATA_HOME=str(home),
                # Scanned as --host h: the tiers read only this host's drives.
                BYTECAT_HOST="h")


def ingest(home, root, uuid):
    """scan + merge one tree as its own volume."""
    env = env_for(home)
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(root), "--host", "h",
                        "--volume-uuid", uuid], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)


def hash_all(home):
    # --hash-all so every file gets a real blob; a size-unique file would
    # otherwise sit at blob_id NULL and the coverage claim would be "unique",
    # which says nothing about whether the audit works.
    return subprocess.run([sys.executable, "-m", "bytecat.hash", "--hash-all"],
                          cwd=ROOT, env=env_for(home), capture_output=True, text=True)


def sql(home, statements):
    """Write directly to the catalog -- used only to plant a lie."""
    code = ("import sys;sys.path.insert(0,'.');from bytecat import db;"
            "c=db.open_catalog();\n" + "\n".join(f"c.execute({s!r})" for s in statements))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env_for(home),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def audit(home, *args):
    r = subprocess.run([sys.executable, "-m", "bytecat.audit", *args],
                       cwd=ROOT, env=env_for(home), capture_output=True, text=True)
    report = ""
    for line in r.stdout.splitlines():
        if "report written to" in line:
            p = Path(line.split("report written to", 1)[1].strip())
            if p.exists():
                report = p.read_text()
    return r.returncode, r.stdout, r.stderr, report


def count(stdout, label):
    for line in stdout.splitlines():
        if label in line:
            return int(line.rsplit(":", 1)[1].split()[0].replace(",", ""))
    return -1


tmp = Path(tempfile.mkdtemp(prefix="bytecat-audit-"))
try:
    # ======================================================== mode 1: coverage
    home = tmp / "home"
    src, tgt = tmp / "src", tmp / "tgt"
    (src).mkdir(parents=True)
    (tgt / "moved").mkdir(parents=True)

    A, B, C = os.urandom(4000), os.urandom(4100), os.urandom(4200)
    (src / "same.bin").write_bytes(A)        # re-filed on the target
    (src / "gone.bin").write_bytes(B)        # genuinely absent there
    (src / "corrupt.bin").write_bytes(C)     # present, same size, wrong bytes
    (tgt / "moved" / "same.bin").write_bytes(A)
    (tgt / "corrupt.bin").write_bytes(os.urandom(4200))

    ingest(home, src, "u-src")
    ingest(home, tgt, "u-tgt")
    h = hash_all(home)
    check("catalog built", h.returncode == 0, h.stderr[-300:])

    rc, out, err, rep = audit(home, "coverage", "--source", str(src),
                              "--target", str(tgt))
    check("clean coverage audit exits 0", rc == 0, out + err[-300:])
    check("re-filed content is present for BOTH (content semantics, not path)",
          count(out, "agree, content present on target") == 1, out)
    check("absent + corrupted are absent for BOTH",
          count(out, "not on target, bytecat did not say copied") == 2, out)
    check("no disagreement on a truthful catalog",
          count(out, "bytecat says copied, disk says no") == 0
          and count(out, "disk has it, bytecat did not say so") == 0, out)
    check("the corrupted file is named in the report", "corrupt.bin" in rep, rep[:400])
    check("the report names the outside check", "hashdeep -a -k" in rep, rep[-600:])

    # ------------------------------------------------- now lie to the catalog
    # Claim gone.bin's content also lives on the target. bytecat will say it is
    # safely copied; the disk says otherwise. This is the failure that would
    # actually cost data, so it is the one the audit must never miss.
    sql(home, [
        "INSERT INTO files (volume_id, relpath, name, ext, size_bytes, mtime, "
        "  inode, nlink, is_symlink, blob_id, t1_hashed, state) "
        "SELECT (SELECT id FROM volumes WHERE uuid='u-tgt'), 'phantom.bin', "
        "  'phantom.bin', 'bin', f.size_bytes, f.mtime, 999999, 1, 0, f.blob_id, 1, "
        "  'present' FROM files f "
        "JOIN volumes v ON v.id = f.volume_id "
        "WHERE v.uuid='u-src' AND f.relpath='gone.bin'",
    ])

    rc, out, err, rep = audit(home, "coverage", "--source", str(src),
                              "--target", str(tgt))
    check("a lying catalog makes the audit DISAGREE",
          count(out, "bytecat says copied, disk says no") == 1, out)
    check("...and exit non-zero", rc == 1, f"rc={rc}")
    check("the verdict comes first and says not to delete",
          "don't delete" in (out.splitlines() or [""])[0], out)
    check("the over-reported file is named", "gone.bin" in rep, rep[:600])
    check("the report calls the verdict unsafe", "disagree" in rep.lower(), rep[:900])

    # ============================================================ mode 2: dupes
    home2 = tmp / "home2"
    d = tmp / "dup"
    d.mkdir(parents=True)
    same = os.urandom(3000)
    (d / "a.bin").write_bytes(same)          # a genuine duplicate pair
    (d / "b.bin").write_bytes(same)
    (d / "c.bin").write_bytes(os.urandom(3500))   # same size, different bytes
    (d / "e.bin").write_bytes(os.urandom(3500))
    m = os.urandom(2500)
    for i in range(4):                       # four identical files
        (d / f"m{i}.bin").write_bytes(m)

    ingest(home2, d, "u-dup")
    h = hash_all(home2)
    check("dupes catalog built", h.returncode == 0, h.stderr[-300:])

    rc, out, err, rep = audit(home2, "dupes", "--path", str(d))
    check("a truthful catalog produces no grouping findings", rc == 0, out + err[-300:])
    check("no groups split", count(out, "GROUPS THAT SPLIT") == 0, out)
    check("no missed duplicates", count(out, "duplicates bytecat MISSED") == 0, out)

    # ------------------------------------------------- two lies about grouping
    #  1. claim c.bin and e.bin are the same content  -> SHA-256 must split them
    #  2. claim m2/m3 are a different content from m0/m1 -> SHA-256 must merge
    sql(home2, [
        "UPDATE files SET blob_id = (SELECT blob_id FROM files WHERE relpath='c.bin') "
        "WHERE relpath='e.bin'",
        "INSERT INTO blobs (hash, size_bytes, quick_hash, n_files) "
        "VALUES ('deadbeef-not-a-real-digest', 2500, 'x', 2)",
        "UPDATE files SET blob_id = (SELECT id FROM blobs "
        "  WHERE hash='deadbeef-not-a-real-digest') "
        "WHERE relpath IN ('m2.bin','m3.bin')",
    ])

    # ...and drop a file the catalog has never seen. If audit ever stopped
    # walking and started trusting bytecat's file list, this goes silent.
    (d / "unseen.bin").write_bytes(os.urandom(1234))

    rc, out, err, rep = audit(home2, "dupes", "--path", str(d))
    check("a split group is caught", count(out, "GROUPS THAT SPLIT") == 1, out)
    check("a duplicate bytecat missed is caught",
          count(out, "duplicates bytecat MISSED") == 1, out)
    check("grouping findings exit non-zero", rc == 1, f"rc={rc}")
    check("the split members are named", "c.bin" in rep and "e.bin" in rep, rep[:900])

    check("a file the catalog never saw is reported (the walk is its own)",
          count(out, "on disk, not in catalog") == 1, out)
    check("...and named in the report", "unseen.bin" in rep, rep[:1200])

    # deletion of a catalogued file must show up too
    os.remove(d / "b.bin")
    rc, out, err, rep = audit(home2, "dupes", "--path", str(d))
    check("a vanished file is reported",
          count(out, "in catalog, not found or skipped on disk") == 1, out)

    # --no-drift must actually stand down
    rc, out, err, rep = audit(home2, "dupes", "--path", str(d), "--no-drift")
    check("--no-drift skips the drift walk",
          count(out, "on disk, not in catalog") == -1, out)

    # ------------------------------------------------------- manifest handoff
    mf = tmp / "m.hashdeep"
    rc, out, err, rep = audit(home2, "dupes", "--path", str(d),
                              "--emit-manifest", str(mf))
    check("hashdeep manifest written", mf.exists() and
          mf.read_text().startswith("%%%% HASHDEEP-1.0"),
          mf.read_text()[:200] if mf.exists() else "missing")

    # ----------------------------------------------------- nothing was touched
    check("audit deleted nothing",
          (d / "a.bin").exists() and (d / "unseen.bin").exists()
          and (src / "gone.bin").exists() and (tgt / "corrupt.bin").exists())
    check("audit names the outside check", "hashdeep -a -k" in rep, rep[-400:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
