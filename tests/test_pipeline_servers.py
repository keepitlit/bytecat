#!/usr/bin/env python3
"""pipeline without a model server or Chroma.

hash and extract need neither, and dupes and coverage need only those. A
script running `bytecat pipeline` on a machine with no model server used to
see exit 1 every time. Now embed is skipped with one line saying why, the
exit code is 0, and --no-embed stops after extract without probing at all.
A non-loopback endpoint is still a failure: that is a setting to fix, not a
server that is down. Endpoints point at a closed port; nothing real is called.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


tmp = Path(tempfile.mkdtemp(prefix="bytecat-servers-"))
try:
    env = dict(os.environ, BYTECAT_HOME=str(tmp / "home"), BYTECAT_HOST="h",
               BYTECAT_LLM_BASE="http://127.0.0.1:9/v1",
               BYTECAT_CHROMA_BASE="http://127.0.0.1:9",
               BYTECAT_LLM_AUTOSTART="0")
    env.pop("BYTECAT_CHROMA_COLLECTION", None)
    src = tmp / "src"
    src.mkdir()
    (src / "a.txt").write_text("same\n")
    (src / "b.txt").write_text("same\n")
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(src), "--volume-uuid", "U"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)

    def pipeline(*args, **extra):
        return subprocess.run([sys.executable, "-m", "bytecat", "pipeline", *args], cwd=ROOT,
                              env={**env, **extra}, capture_output=True, text=True)

    r = pipeline()
    check("no model server: exit 0", r.returncode == 0, r.stdout + r.stderr)
    out = r.stdout + r.stderr
    check("hash and extract ran", "hashed 2" in out and "described 2" in out, out)
    check("one line says embed was skipped and why",
          "embed skipped: no model server or model at http://127.0.0.1:9/v1" in r.stderr,
          r.stderr)
    check("no failure line", "failed" not in r.stderr, r.stderr)

    r = pipeline("--no-embed")
    check("--no-embed: exit 0", r.returncode == 0, r.stdout + r.stderr)
    check("--no-embed: embed never starts",
          "=== extract ===" in r.stdout and "embed" not in r.stdout + r.stderr,
          r.stdout + r.stderr)

    r = pipeline(BYTECAT_LLM_BASE="http://10.1.2.3:1/v1")
    check("a non-loopback endpoint still fails", r.returncode == 1
          and "refusing non-loopback" in r.stderr, r.stdout + r.stderr)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
