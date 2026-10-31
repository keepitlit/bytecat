#!/usr/bin/env python3
"""The tiers read only this machine's drives.

A drive scanned on another host is that host's bytes. If its scan path also
exists here (a shared path name, a synced copy), reading the local files would
fingerprint something else under its name. hash, extract, embed and enrich
must skip it; --emit-work is how it gets hashed.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


tmp = Path(tempfile.mkdtemp(prefix="bytecat-host-"))
try:
    os.environ["BYTECAT_HOME"] = str(tmp / "home")
    os.environ["BYTECAT_HOST"] = "here"
    env = dict(os.environ)
    for name, host in (("mine", "here"), ("theirs", "elsewhere")):
        d = tmp / name
        d.mkdir()
        (d / "same.txt").write_text("identical bytes on both drives\n")
        args = ["--host", host] if host != "here" else []
        s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(d), "--volume-uuid",
                            f"U-{name}", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                       input=s.stdout, capture_output=True, text=True)

    from bytecat import db, embed, enrich
    conn = db.connect()
    hosts = {r[0]: r[1] for r in conn.execute(
        "SELECT v.uuid, h.name FROM volumes v JOIN hosts h ON h.id = v.host_id")}
    check("scan's default host is BYTECAT_HOST", hosts.get("U-mine") == "here", str(hosts))

    for tier in ("hash", "extract"):
        subprocess.run([sys.executable, "-m", f"bytecat.{tier}"], cwd=ROOT, env=env,
                       capture_output=True, text=True)

    def flags(uuid):
        return tuple(conn.execute(
            "SELECT f.t1_hashed, f.t2_meta FROM files f JOIN volumes v ON v.id = f.volume_id "
            "WHERE v.uuid = ?", (uuid,)).fetchone())

    check("this host's drive is hashed and described", flags("U-mine") == (1, 1),
          str(flags("U-mine")))
    check("another host's drive is not read, though its path exists here",
          flags("U-theirs") == (0, 0), str(flags("U-theirs")))
    check("embed would not open it",
          [r["id"] for r in embed.pending(conn, None, 0)] ==
          [r[0] for r in conn.execute("SELECT f.id FROM files f JOIN volumes v "
                                      "ON v.id = f.volume_id WHERE v.uuid = 'U-mine'")])
    check("nor would enrich",
          all("mine" in r["path"] for r in enrich.candidates(conn, ["document"], 10, False)))

    w = subprocess.run([sys.executable, "-m", "bytecat.hash", "--emit-work", "elsewhere"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    jobs = json.loads(w.stdout)["jobs"]
    check("--emit-work lists it for its own host", len(jobs) == 1, w.stdout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
