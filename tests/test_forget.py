#!/usr/bin/env python3
"""forget removes a drive or a host from the catalog, and nothing else.

It asks first unless --yes is given, refuses on anything but yes, lists what
it removed, and never opens, moves or deletes a file on disk. Fingerprints no
remaining file uses go with it; shared ones keep their other copies. A search
vector left in Chroma for a forgotten file must not surface under a new file
that later reuses its id.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def tree(path):
    return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in Path(path).rglob("*"))


tmp = Path(tempfile.mkdtemp(prefix="bytecat-forget-"))
try:
    os.environ["BYTECAT_HOME"] = str(tmp / "home")
    os.environ["BYTECAT_HOST"] = "laptop"
    env = dict(os.environ)

    def run(*args, stdin=""):
        return subprocess.run([sys.executable, "-m", "bytecat", *args], cwd=ROOT, env=env,
                              input=stdin, capture_output=True, text=True)

    def scan(path, uuid, *args):
        s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(path), "--volume-uuid",
                            uuid, *args], cwd=ROOT, env=env, capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                       input=s.stdout, capture_output=True, text=True)

    docs, usb, nas = tmp / "docs", tmp / "usb", tmp / "nas"
    for d in (docs, usb, nas):
        d.mkdir()
    shared = "the same bytes on two drives\n"
    (docs / "kept.txt").write_text(shared)
    (usb / "kept.txt").write_text(shared)
    (usb / "zebra-only-on-usb.txt").write_text("only here, " * 3)
    (usb / "size-twin.txt").write_text("x" * 33)          # shares a size, so it is read
    (nas / "nas-file.txt").write_text("on the nas\n")
    scan(docs, "DOCS")
    scan(usb, "USB")
    scan(nas, "NAS", "--host", "nas")
    run("hash")
    run("extract")
    before = tree(tmp / "docs") + tree(tmp / "usb") + tree(tmp / "nas")

    from bytecat import ask, db
    conn = db.connect()

    def count(sql, *p):
        return conn.execute(sql, p).fetchone()[0]

    vols = lambda: sorted(r[0] for r in conn.execute("SELECT uuid FROM volumes"))

    print("\n[asking first]")
    r = run("forget", "--volume", "USB", stdin="n\n")
    check("a no keeps everything", r.returncode == 1 and vols() == ["DOCS", "NAS", "USB"],
          r.stdout + r.stderr)
    check("the question lists the drive first", "USB" in r.stdout and "3 files" in r.stdout,
          r.stdout)
    r = run("forget", "--volume", "USB")
    check("no answer at all keeps everything", r.returncode == 1 and len(vols()) == 3,
          r.stdout + r.stderr)
    r = run("forget")
    check("neither --volume nor --host is a usage error", r.returncode == 2, r.stderr)
    r = run("forget", "--volume", "NOPE", "--yes")
    check("an unknown drive removes nothing", r.returncode != 0 and len(vols()) == 3, r.stderr)

    print("\n[forget a drive]")
    usb_blob = count("SELECT f.blob_id FROM files f JOIN volumes v ON v.id = f.volume_id "
                     "WHERE v.uuid = 'USB' AND f.name = 'size-twin.txt'")
    r = run("forget", "--volume", "USB", "--yes")
    check("--yes removes it without asking", r.returncode == 0 and vols() == ["DOCS", "NAS"],
          r.stdout + r.stderr)
    check("and says what it removed", "USB" in r.stdout and "3 files" in r.stdout, r.stdout)
    check("its files, descriptions and errors are gone",
          count("SELECT COUNT(*) FROM files WHERE volume_id NOT IN (SELECT id FROM volumes)") == 0
          and count("SELECT COUNT(*) FROM extractions WHERE file_id NOT IN "
                    "(SELECT id FROM files)") == 0)
    check("keyword search no longer finds them",
          count("SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH 'zebra'") == 0)
    check("a fingerprint only it used is gone",
          usb_blob and count("SELECT COUNT(*) FROM blobs WHERE id = ?", usb_blob) == 0,
          str(usb_blob))
    check("a shared one now counts one copy",
          count("SELECT b.n_files FROM blobs b JOIN files f ON f.blob_id = b.id "
                "WHERE f.name = 'kept.txt'") == 1)

    print("\n[forget a host]")
    r = run("forget", "--host", "nas", stdin="y\n")
    check("a yes removes the host and its drives",
          r.returncode == 0 and vols() == ["DOCS"]
          and count("SELECT COUNT(*) FROM hosts WHERE name = 'nas'") == 0, r.stdout + r.stderr)
    r = run("forget", "--host", "nas", "--yes")
    check("an unknown host removes nothing", r.returncode != 0 and vols() == ["DOCS"], r.stderr)

    check("no file on disk was touched",
          tree(tmp / "docs") + tree(tmp / "usb") + tree(tmp / "nas") == before)

    print("\n[a stale search vector]")
    fid, size = conn.execute("SELECT id, size_bytes FROM files WHERE name = 'kept.txt'"
                             ).fetchone()
    check("a vector made for this file is kept",
          ask.current(conn, [(fid, {"size": size})]) == [fid])
    check("one made for a forgotten file under the same id is dropped",
          ask.current(conn, [(fid, {"size": size + 1})]) == [])
    conn.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
