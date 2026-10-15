#!/usr/bin/env python3
"""Prove report_coverage can report MISSING, not just PRESENT.

A checker only ever observed saying "all present" has proved nothing. This
seeds a source tree where some files ARE on the target and some are NOT, and
asserts both verdicts land in the right bucket.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))

tmp = Path(tempfile.mkdtemp(prefix="bytecat-cov-"))
try:
    src, tgt = tmp / "src", tmp / "tgt"
    src.mkdir(); tgt.mkdir()
    (src / "shared").mkdir(); (src / "orphans").mkdir()
    (tgt / "renamed").mkdir()

    # Present on target, under a DIFFERENT path -- must still count as present.
    for i in range(3):
        body = os.urandom(4096) + f"shared-{i}".encode() * 500
        (src / "shared" / f"s{i}.bin").write_bytes(body)
        (tgt / "renamed" / f"totally-different-name-{i}.bin").write_bytes(body)

    # Absent from target. Distinct sizes so tier 1 marks them size-unique.
    for i in range(2):
        (src / "orphans" / f"o{i}.bin").write_bytes(os.urandom(7000 + i * 1301))

    # Absent from target but size-COLLIDES with a target file of different
    # content -- the case a size-only prefilter would get wrong.
    same = 9999
    (src / "orphans" / "collide.bin").write_bytes(b"A" * same)
    (tgt / "renamed" / "collide-other.bin").write_bytes(b"B" * same)

    env = dict(os.environ, BYTECAT_HOME=str(tmp / "home"), XDG_DATA_HOME=str(tmp / "home"))
    def run(cmd):
        return subprocess.run([sys.executable, "-m"] + cmd, cwd=ROOT, env=env,
                              capture_output=True, text=True)

    for root, host, uuid in ((src, "srchost", "u-src"), (tgt, "tgthost", "u-tgt")):
        s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(root),
                            "--host", host, "--volume-uuid", uuid],
                           cwd=ROOT, env=env, capture_output=True, text=True)
        m = subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                           input=s.stdout, capture_output=True, text=True)
        check(f"scan+merge {host}", s.returncode == 0 and m.returncode == 0,
              (s.stderr + m.stderr)[-300:])

    h = run(["bytecat.hash"])
    check("hash run", h.returncode == 0, h.stderr[-300:])

    r = run(["bytecat.report_coverage", "--source-volume", "u-src", "--target-host", "tgthost"])
    check("coverage run", r.returncode == 0, r.stderr[-400:])
    out = r.stdout
    print("     " + " | ".join(l.strip() for l in out.splitlines() if "files" in l))

    # The bucket checks call classify() in-process, so they pin what lands in
    # each bucket, not the words the terminal prints for it.
    from bytecat import db
    from bytecat.report_coverage import classify, resolve_volumes
    conn = db.connect(tmp / "home" / "catalog.db", readonly=True)
    res = classify(conn, resolve_volumes(conn, ["u-src"], [])[0],
                   resolve_volumes(conn, [], ["tgthost"]))
    conn.close()

    def bucket(n):
        return sum(v[0] for v in res["buckets"].get(n, {}).values())

    check("3 renamed files found PRESENT", bucket("present") == 3, f"got {bucket('present')}")
    check("checker CAN report absent", bucket("missing") + bucket("unique") == 3,
          f"missing={bucket('missing')} unique={bucket('unique')}")
    check("size-collision not miscounted as present",
          bucket("present") == 3 and bucket("missing") >= 1,
          f"present={bucket('present')} missing={bucket('missing')}")
    check("nothing left unhashed", bucket("unhashed") == 0, f"got {bucket('unhashed')}")

    # The privacy invariant this tool must hold: no paths on stdout.
    check("no source paths on stdout",
          "collide.bin" not in out and "o0.bin" not in out and "s0.bin" not in out)

    rep = list((tmp / "home" / "bytecat" / "reports").glob("coverage-*.md")) or \
          list((tmp / "home").rglob("coverage-*.md"))
    check("report file written", bool(rep))
    if rep:
        body = rep[0].read_text()
        check("missing paths ARE in the report file", "collide.bin" in body)

    # Source files must all still exist.
    check("no source file removed", len(list((src).rglob("*.bin"))) == 6)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
