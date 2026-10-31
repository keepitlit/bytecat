#!/usr/bin/env python3
"""eval's default query set ships with the package, not only with a clone.

Copies bytecat/ alone into a temporary site directory, as an install would,
and runs eval from there: the bundled set must load without tests/.
"""
import os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))

tmp = Path(tempfile.mkdtemp(prefix="bytecat-eval-"))
try:
    site = tmp / "site"
    shutil.copytree(ROOT / "bytecat", site / "bytecat",
                    ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ, BYTECAT_HOME=str(tmp / "home"), PYTHONPATH=str(site))
    r = subprocess.run([sys.executable, "-m", "bytecat.eval"], cwd=tmp, env=env,
                       capture_output=True, text=True)
    # No catalog in BYTECAT_HOME: exit 2 means the query set loaded first.
    check("default query set loads from an installed copy",
          r.returncode == 2 and "Traceback" not in r.stderr, r.stderr[-300:])
    pyproject = (ROOT / "pyproject.toml").read_text()
    data = re.search(r"\[tool\.setuptools\.package-data\]\s*bytecat = \[([^\]]*)\]", pyproject)
    check("pyproject ships it as package data",
          bool(data) and "eval_queries.json" in data.group(1))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
