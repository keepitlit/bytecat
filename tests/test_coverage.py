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

    # This machine is srchost; tgthost hashes its own files, as a NAS would.
    env = dict(os.environ, BYTECAT_HOME=str(tmp / "home"), XDG_DATA_HOME=str(tmp / "home"),
               BYTECAT_HOST="srchost")
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
    (tmp / "work.json").write_text(run(["bytecat.hash", "--emit-work", "tgthost"]).stdout)
    (tmp / "results.ndjson").write_text(
        run(["bytecat.scan", "--hash-list", str(tmp / "work.json")]).stdout)
    i = run(["bytecat.hash", "--ingest", str(tmp / "results.ndjson")])
    check("tgthost's files hashed there and ingested", i.returncode == 0, i.stderr[-300:])

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
    # A drive can be named by its mount; a subfolder of it is refused, not
    # widened to the whole drive.
    by_mount = resolve_volumes(conn, [str(src)], [])
    check("scanned root resolves to its volume",
          by_mount == resolve_volumes(conn, ["u-src"], []), f"got {by_mount}")
    try:
        resolve_volumes(conn, [str(src / "shared")], [])
        refused = False
    except SystemExit:
        refused = True
    check("a subfolder of a mount is refused", refused)
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

    def scan_merge(root, uuid):
        s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(root),
                            "--volume-uuid", uuid], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                       input=s.stdout, capture_output=True, text=True)

    def report_body(r):
        line = [l for l in r.stdout.splitlines() if "report written to" in l]
        return Path(line[-1].split("report written to ", 1)[1]).read_text() if line else ""

    # A file hash could not open is no proof it has no copy: it was never read.
    if os.geteuid() == 0:
        print("  SKIP  unreadable file checks (root reads everything)")
    else:
        src2 = tmp / "src2"
        src2.mkdir()
        # Same size, so tier 1 must open both; one of them it cannot.
        (src2 / "locked.bin").write_bytes(b"L" * 5555)
        (src2 / "twin.bin").write_bytes(b"T" * 5555)
        (src2 / "locked.bin").chmod(0)
        try:
            scan_merge(src2, "u-src2")
            run(["bytecat.hash"])
            conn = db.connect(tmp / "home" / "catalog.db", readonly=True)
            res2 = classify(conn, resolve_volumes(conn, ["u-src2"], [])[0],
                            resolve_volumes(conn, [], ["tgthost"]))
            conn.close()
            why = {p: w for p, _, w in res2["missing_paths"]}
            check("an unreadable file is not called unique",
                  why.get("locked.bin") not in ("unique", "missing"), str(why))
            r = run(["bytecat.report_coverage", "--source-volume", "u-src2",
                     "--target-host", "tgthost"])
            body = report_body(r)
            check("it is listed as unreadable",
                  "## Could not be read" in body and "| locked.bin | 5,555 | unreadable |" in body
                  and "bytecat hash --volume u-src2 --forget" in body, body[-600:])
        finally:
            (src2 / "locked.bin").chmod(0o644)

    # A copy on a target that is not plugged in now is not a confirmed copy.
    usb = tmp / "usb"
    usb.mkdir()
    shutil.copy(src / "orphans" / "o0.bin", usb / "o0-copy.bin")
    scan_merge(usb, "u-usb")
    run(["bytecat.hash"])

    # classify() runs in this process: it must judge mounts as srchost.
    os.environ["BYTECAT_HOST"] = "srchost"

    def o0_bucket():
        conn = db.connect(tmp / "home" / "catalog.db", readonly=True)
        res3 = classify(conn, resolve_volumes(conn, ["u-src"], [])[0],
                        resolve_volumes(conn, ["u-usb"], []))
        conn.close()
        return {n: res3["buckets"][n].get("orphans", [0])[0] for n in res3["buckets"]}

    got = o0_bucket()
    check("a copy on a mounted target is counted present",
          got.get("present") == 1, str(got))
    shutil.move(str(usb), str(tmp / "usb-unplugged"))
    got = o0_bucket()
    check("a copy on an unmounted target is not counted present",
          not got.get("present") and got.get("present_offline") == 1, str(got))
    r = run(["bytecat.report_coverage", "--source-volume", "u-src", "--target-volume", "u-usb"])
    body = report_body(r)
    check("the report names the offline target and its last scan",
          "## Only on an offline target" in body and "last scanned 20" in body, body[-600:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
