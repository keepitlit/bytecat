#!/usr/bin/env python3
"""Prove --hash-all actually reads what tier 1 skips, and changes nothing else.

Tier 1 closes out a size-unique file with blob_id NULL and no hash ever
computed. --hash-all must instead give it a real blob. A flag only ever
observed on a tree where it makes no difference has proved nothing, so this
seeds files that are DELIBERATELY size-unique and asserts the two modes
disagree about them -- and agree about everything else.
"""
import os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def seed(root):
    """3 size-unique orphans, 2 real duplicates, 1 symlink, 1 zero-byte."""
    root.mkdir(parents=True)
    for i in range(3):                                   # unique sizes
        (root / f"uniq{i}.bin").write_bytes(os.urandom(5000 + i * 977))
    body = os.urandom(3000)
    (root / "dupA.bin").write_bytes(body)                # same content
    (root / "dupB.bin").write_bytes(body)                # -> one blob, 2 files
    (root / "empty.bin").write_bytes(b"")
    os.symlink("dupA.bin", root / "link.bin")


def catalog(tmp, mode):
    """Scan + merge + hash a fresh tree in its own catalog.

    Return (rows, stderr, returncode, summary counts)."""
    home = tmp / f"home-{mode}"
    src = tmp / f"src-{mode}"
    seed(src)
    env = dict(os.environ, BYTECAT_HOME=str(home), XDG_DATA_HOME=str(home))
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(src), "--host", "h",
                        "--volume-uuid", "u-1"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)
    cmd = [sys.executable, "-m", "bytecat.hash"] + (["--hash-all"] if mode == "all" else [])
    h = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    dump = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,'.');from bytecat import db;"
         "c=db.open_catalog();"
         "print('\\n'.join(f\"{r['relpath']}|{r['t1_hashed']}|{r['blob_id'] is not None}\""
         " for r in c.execute('SELECT relpath,t1_hashed,blob_id FROM files ORDER BY relpath')))"],
        cwd=ROOT, env=env, capture_output=True, text=True)
    rows = {}
    for line in dump.stdout.strip().splitlines():
        p, t1, has_blob = line.split("|")
        rows[p] = (t1 == "1", has_blob == "True")
    return rows, h.stderr, h.returncode, summary_counts(h.stdout)


def summary_counts(out):
    """The hash summary's numbers in order: hashed, reused, need no read, unreadable.

    Read by position, so the assertions hold the counts and not the sentence."""
    for line in out.splitlines():
        if line.startswith("[bytecat] hashed"):
            return [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", line)]
    return []


tmp = Path(tempfile.mkdtemp(prefix="bytecat-hashall-"))
try:
    t1, t1_err, t1_rc, t1_counts = catalog(tmp, "tier1")
    al, al_err, al_rc, al_counts = catalog(tmp, "all")

    check("tier 1 run", t1_rc == 0, t1_err[-300:])
    check("--hash-all run", al_rc == 0, al_err[-300:])
    check("both catalogued the same files", set(t1) == set(al),
          f"{set(t1) ^ set(al)}")

    uniq = [f"uniq{i}.bin" for i in range(3)]

    # The whole point of the flag.
    check("tier 1 leaves size-unique files UNHASHED",
          all(t1[u] == (True, False) for u in uniq),
          f"{ {u: t1[u] for u in uniq} }")
    check("--hash-all gives size-unique files a real blob",
          all(al[u] == (True, True) for u in uniq),
          f"{ {u: al[u] for u in uniq} }")
    check("the two modes genuinely disagree",
          any(t1[u] != al[u] for u in uniq))

    # Everything else must be untouched by the flag.
    for d in ("dupA.bin", "dupB.bin"):
        check(f"{d} hashed in BOTH modes", t1[d] == (True, True) == al[d],
              f"tier1={t1[d]} all={al[d]}")
    check("real duplicates still collapse to one blob under --hash-all",
          al["dupA.bin"] == al["dupB.bin"] == (True, True))
    # scan.py --min-size defaults to 1, so a zero-byte file never enters the
    # catalog at all -- in either mode. Pinned because it is the reason a raw
    # `find | wc -l` will always exceed the catalogued count.
    check("zero-byte file is not catalogued in either mode",
          "empty.bin" not in al and "empty.bin" not in t1,
          f"all={al.get('empty.bin')} tier1={t1.get('empty.bin')}")
    check("symlink still closed out with no blob under --hash-all",
          al["link.bin"] == (True, False), f"{al.get('link.bin')}")

    # The summary must report what was read and what was closed out unread:
    # under --hash-all only the symlink needs no read; tier 1 also skips the
    # three size-unique files.
    check("--hash-all reports 5 hashed, 1 needing no read",
          len(al_counts) == 4 and al_counts[0] == 5 and al_counts[2] == 1,
          f"{al_counts}")
    check("tier 1 reports 2 hashed, 4 needing no read",
          len(t1_counts) == 4 and t1_counts[0] == 2 and t1_counts[2] == 4,
          f"{t1_counts}")

    # source tree untouched
    check("no source file removed",
          len(list((tmp / "src-all").iterdir())) == 7)
finally:
    import shutil; shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
