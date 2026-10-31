#!/usr/bin/env python3
"""Two reports of one kind never share a name, even inside one second."""
import os, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))

tmp = Path(tempfile.mkdtemp(prefix="bytecat-rp-"))
try:
    os.environ["BYTECAT_HOME"] = str(tmp)
    from bytecat import db
    names = []
    for _ in range(3):
        p = db.report_path("coverage")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        names.append(p.name)
    check("three runs in one second get three names", len(set(names)) == 3, str(names))
    check("named to the second", len(names[0].split("-")[2].split(".")[0]) == 6, names[0])
    check("--out is used as given", db.report_path("coverage", str(tmp / "a.md")) == tmp / "a.md")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
