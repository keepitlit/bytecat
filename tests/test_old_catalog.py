#!/usr/bin/env python3
"""A catalog made before volumes.fs_uuid, volumes.vbase and files.last_run
opens without a traceback: status, the tier pre-check and ask add the columns."""
import os, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))

def run(env, *args):
    return subprocess.run([sys.executable, "-m", "bytecat", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)

def make_old(home):
    """Seed a catalog, then drop the columns an older bytecat did not have."""
    env = dict(os.environ, BYTECAT_HOME=str(home))
    src = home.parent / "src"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("a\n")
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(src), "--host", "h",
                        "--volume-uuid", "u-1"], cwd=ROOT, env=env, capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)
    conn = sqlite3.connect(home / "catalog.db")
    for t, c in (("volumes", "fs_uuid"), ("volumes", "vbase"), ("files", "last_run")):
        conn.execute(f"ALTER TABLE {t} DROP COLUMN {c}")
    conn.commit()
    conn.close()
    return env

def cols(home, table):
    conn = sqlite3.connect(home / "catalog.db")
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()

if sqlite3.sqlite_version_info < (3, 35, 0):
    print("  SKIP  needs SQLite 3.35 for DROP COLUMN")
    sys.exit(0)

tmp = Path(tempfile.mkdtemp(prefix="bytecat-oldcat-"))
try:
    for verb in (["status"], ["embed", "-h"], ["ask", "a.txt"]):
        home = tmp / verb[0] / "home"
        home.mkdir(parents=True)
        env = make_old(home)
        check(f"{verb[0]}: old catalog has no fs_uuid", "fs_uuid" not in cols(home, "volumes"))
        r = run(env, *verb)
        check(f"{verb[0]}: no traceback", "Traceback" not in r.stderr, r.stderr[-400:])
        check(f"{verb[0]}: columns added",
              {"fs_uuid", "vbase"} <= cols(home, "volumes") and "last_run" in cols(home, "files"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
