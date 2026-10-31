#!/usr/bin/env python3
"""Several scan roots on one drive coexist, and a drive that remounts is still one drive.

Two roots on one drive share its identity. Same-named files under each must
stay two rows, a rescan of one root must never mark the other's files
missing, and every stored path must still open on disk. A catalog made before
roots were placed this way (no vbase column, paths relative to the last root)
must keep its files when a second root arrives.
"""
import io, json, os, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


tmp = Path(tempfile.mkdtemp(prefix="bytecat-roots-"))
os.environ["BYTECAT_HOME"] = str(tmp / "home")
from bytecat import db, merge


def vol(root, mount=None, uuid="U-1", host="h"):
    rec = {"type": "volume", "host": host, "uuid": uuid, "root": root}
    if mount is not None:
        rec["mount"] = mount
    return rec


def f(relpath, size=10, mtime=1):
    return {"type": "file", "relpath": relpath, "name": relpath.rsplit("/", 1)[-1],
            "ext": "txt", "size": size, "mtime": mtime, "inode": 0, "nlink": 1, "symlink": 0}


def feed(conn, *recs):
    return merge.merge_stream(conn, io.StringIO("".join(json.dumps(r) + "\n" for r in recs)))


def present(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT relpath FROM files WHERE state = 'present'"))


def base(conn):
    return conn.execute("SELECT last_mount FROM volumes").fetchone()[0]


def fresh(name):
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(p)
    db.init_db(conn)
    return conn


try:
    print("\n[two roots on one drive]")
    conn = fresh("a.db")
    feed(conn, vol("/m/home/docs", "/m"), f("notes.txt"), f("a.txt"))
    feed(conn, vol("/m/home/pics", "/m"), f("notes.txt"), f("b.txt"))
    check("same-named files under two roots are two rows",
          present(conn) == ["docs/a.txt", "docs/notes.txt", "pics/b.txt", "pics/notes.txt"],
          str(present(conn)))
    check("paths are relative to the folder holding both roots", base(conn) == "/m/home",
          base(conn))
    s = feed(conn, vol("/m/home/docs", "/m"), f("notes.txt"))
    check("a rescan of one root prunes only under it",
          s["missing"] == 1 and present(conn) == ["docs/notes.txt", "pics/b.txt",
                                                  "pics/notes.txt"], str(present(conn)))
    s = feed(conn, vol("/m/home/pics", "/m"), f("notes.txt"), f("b.txt"))
    check("and never marks the other root's files missing",
          s["missing"] == 0 and "docs/notes.txt" in present(conn), str(present(conn)))
    s = feed(conn, vol("/m/home", "/m"), f("docs/notes.txt"), f("pics/notes.txt"),
             f("pics/b.txt"))
    check("a root holding both maps to the same rows",
          s["missing"] == 0 and conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 4,
          str(present(conn)))
    check("FTS follows the re-prefixed paths",
          conn.execute("SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH '\"pics\"'"
                       ).fetchone()[0] == 2)

    print("\n[two roots in one scan]")
    conn = fresh("b.db")
    feed(conn, vol("/m/x", "/m"), f("same.txt"), vol("/m/y", "/m"), f("same.txt"))
    check("both kept", present(conn) == ["x/same.txt", "y/same.txt"], str(present(conn)))

    print("\n[a remount is the same drive]")
    conn = fresh("c.db")
    feed(conn, vol("/Volumes/Backup", "/Volumes/Backup"), f("a.txt"))
    s = feed(conn, vol("/Volumes/Backup 1", "/Volumes/Backup 1"), f("a.txt"))
    check("paths unchanged, nothing missing",
          present(conn) == ["a.txt"] and s["missing"] == 0, str(present(conn)))
    check("the folder follows the mount", base(conn) == "/Volumes/Backup 1", base(conn))
    feed(conn, vol("/Volumes/Backup/Photos", "/Volumes/Backup"), f("p.jpg"))
    check("a subfolder after a remount is placed inside the drive",
          base(conn) == "/Volumes/Backup" and "Photos/p.jpg" in present(conn)
          and "a.txt" in present(conn), f"{base(conn)} {present(conn)}")

    print("\n[a catalog from before roots were placed]")
    schema = db.SCHEMA_PATH.read_text()
    for col in ("    fs_uuid      TEXT,\n", "    vbase        TEXT,\n"):
        assert col in schema, col
        schema = schema.replace(col, "")
    legacy = tmp / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.executescript(schema)
    raw.execute("INSERT INTO hosts (name) VALUES ('h')")
    raw.execute("INSERT INTO volumes (host_id, uuid, last_mount) VALUES (1, 'U-1', '/m/home/docs')")
    raw.execute("INSERT INTO files (volume_id, relpath, name, size_bytes, mtime) "
                "VALUES (1, 'notes.txt', 'notes.txt', 10, 1)")
    raw.commit()
    raw.close()
    conn = db.connect(legacy)
    db.init_db(conn)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(volumes)")}
    check("migration adds the new columns", {"vbase", "fs_uuid"} <= cols, str(cols))
    s = feed(conn, vol("/m/home/pics", "/m"), f("notes.txt"))
    check("its files survive a second root, re-prefixed",
          s["missing"] == 0 and present(conn) == ["docs/notes.txt", "pics/notes.txt"],
          str(present(conn)))

    print("\n[a scan.py too old to send the mount]")
    conn = fresh("d.db")
    feed(conn, vol("/m/home/docs"), f("a.txt"))
    s = feed(conn, vol("/m/home/docs/sub"), f("b.txt"))
    check("a root inside the folder is placed in it",
          present(conn) == ["a.txt", "sub/b.txt"] and s["missing"] == 0, str(present(conn)))

    print("\n[end to end: scan, merge, hash read every root's files]")
    drive = tmp / "drive"
    for sub in ("one", "two"):
        (drive / sub).mkdir(parents=True)
        (drive / sub / "same.txt").write_text(f"{sub} content, same length\n")
    env = dict(os.environ, BYTECAT_HOME=str(tmp / "e2e"))
    for sub in ("one", "two"):
        s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(drive / sub),
                            "--volume-uuid", "E2E"], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                       input=s.stdout, capture_output=True, text=True)
    h = subprocess.run([sys.executable, "-m", "bytecat.hash"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    conn = db.connect(tmp / "e2e" / "catalog.db")
    rows = conn.execute("SELECT f.relpath, f.blob_id, v.last_mount FROM files f "
                        "JOIN volumes v ON v.id = f.volume_id WHERE f.state = 'present'"
                        ).fetchall()
    check("both roots catalogued", sorted(r[0] for r in rows) == ["one/same.txt", "two/same.txt"],
          str([tuple(r) for r in rows]))
    check("every stored path opens", all(os.path.isfile(os.path.join(r[2], r[0])) for r in rows))
    check("hash read both", all(r[1] for r in rows) and rows[0][1] != rows[1][1],
          h.stderr[-300:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
