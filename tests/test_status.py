#!/usr/bin/env python3
"""status shows scan dates and run times in local time, as reports do."""
import os, shutil, sqlite3, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))

tmp = Path(tempfile.mkdtemp(prefix="bytecat-status-"))
try:
    src = tmp / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    # Far from UTC, so a UTC time cannot pass for a local one.
    env = dict(os.environ, BYTECAT_HOME=str(tmp / "home"), TZ="Pacific/Kiritimati")
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(src), "--host", "h",
                        "--volume-uuid", "u-1"], cwd=ROOT, env=env, capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)
    # A scan stored just before midnight UTC is the next day in UTC+14.
    conn = sqlite3.connect(tmp / "home" / "catalog.db")
    conn.execute("UPDATE volumes SET last_scan = '2026-10-30 23:30:00'")
    conn.execute("UPDATE runs SET started_at = '2026-10-30 23:30:05'")
    conn.commit()
    conn.close()
    out = subprocess.run([sys.executable, "-m", "bytecat", "status"], cwd=ROOT, env=env,
                         capture_output=True, text=True).stdout
    check("no UTC label", "UTC" not in out, out)
    check("scan date is local", "scanned 2026-10-31" in out, out)
    check("run time is local", "2026-10-31 13:30:05" in out, out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
