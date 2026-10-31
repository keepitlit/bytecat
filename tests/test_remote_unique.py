#!/usr/bin/env python3
"""A remote host's files are closed out the way local ones are.

hash --emit-work HOST used to list only files sharing a size, and left the
host's unique-size files unhashed forever: status stayed below 100% and
coverage from that host reported them "not hashed yet". A unique size is a
catalog-wide fact, so they are closed out without being read, as hash does
locally. And because that fact can stop being true, a file closed out by size
is re-opened once another file in the catalog shares its size, on either host.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


tmp = Path(tempfile.mkdtemp(prefix="bytecat-remote-"))
try:
    env = dict(os.environ, BYTECAT_HOME=str(tmp / "home"), BYTECAT_HOST="laptop")

    def run(*args):
        return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    def scan(path, *args):
        s = run("bytecat.scan", str(path), *args)
        run_m = subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                               input=s.stdout, capture_output=True, text=True)
        return run_m

    def remote_hash(host):
        w = run("bytecat.hash", "--emit-work", host)
        (tmp / "work.json").write_text(w.stdout)
        (tmp / "out.ndjson").write_text(run("bytecat.scan", "--hash-list",
                                            str(tmp / "work.json")).stdout)
        run("bytecat.hash", "--ingest", str(tmp / "out.ndjson"))
        return w

    laptop, nas = tmp / "laptop", tmp / "nas"
    laptop.mkdir()
    nas.mkdir()
    shared = os.urandom(4000)
    (laptop / "kept.bin").write_bytes(shared)
    (nas / "kept-copy.bin").write_bytes(shared)          # same content, other host
    (nas / "only-on-nas.bin").write_bytes(os.urandom(7777))   # unique size
    (laptop / "early.bin").write_bytes(b"e" * 5555)            # unique, until later

    scan(laptop, "--volume-uuid", "LAPTOP")
    scan(nas, "--volume-uuid", "NAS", "--host", "nas")
    run("bytecat.hash")
    w = remote_hash("nas")
    check("--emit-work lists only the file that shares a size", '"kept-copy.bin"' in w.stdout
          and "only-on-nas" not in w.stdout, w.stdout)
    check("and says how many need no read", "1 need no read" in w.stderr, w.stderr)

    from bytecat import db
    from bytecat.report_coverage import classify, resolve_volumes
    conn = db.connect(tmp / "home" / "catalog.db")
    unhashed = conn.execute("SELECT COUNT(*) FROM files WHERE state = 'present' "
                            "AND t1_hashed = 0").fetchone()[0]
    check("every file is closed out: status reaches 100%", unhashed == 0, f"{unhashed} left")
    res = classify(conn, resolve_volumes(conn, ["NAS"], [])[0], resolve_volumes(conn, [], ["laptop"]))
    bucket = {k: sum(v[0] for v in res["buckets"].get(k, {}).values())
              for k in ("present", "missing", "unique", "unhashed")}
    check("coverage from the NAS: the unique file is not on the laptop, by size",
          bucket == {"present": 1, "missing": 0, "unique": 1, "unhashed": 0}, str(bucket))

    print("\n[a size closed out earlier, shared later]")
    (nas / "early-copy.bin").write_bytes(b"e" * 5555)
    scan(nas, "--volume-uuid", "NAS", "--host", "nas")
    w = remote_hash("nas")
    check("the new file is listed", "early-copy.bin" in w.stdout, w.stdout)
    h = run("bytecat.hash")
    check("the laptop file closed out by size is read again", "hashed 1;" in h.stdout,
          h.stdout + h.stderr)
    early = conn.execute("SELECT blob_id FROM files WHERE relpath = 'early.bin'").fetchone()[0]
    copy = conn.execute("SELECT blob_id FROM files WHERE relpath = 'early-copy.bin'").fetchone()[0]
    check("and found to be the same content as the NAS file", early and early == copy,
          f"{early} {copy}")
    h = run("bytecat.hash")
    check("a second run reads nothing", "hashed 0;" in h.stdout, h.stdout)
    conn.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
