#!/usr/bin/env python3
"""How a drive is identified, and recognised again when it is mounted here.

- On Linux the filesystem UUID is read from /proc/self/mountinfo and
  /dev/disk/by-uuid (faked here, so this runs on any OS), and a drive
  catalogued before that under a marker or synthetic ID keeps its history.
- A drive scanned with --volume-uuid on this machine reads online while it
  is mounted, though the drive itself reports a different ID.
"""
import io, json, os, platform, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


tmp = Path(tempfile.mkdtemp(prefix="bytecat-id-"))
os.environ["BYTECAT_HOME"] = str(tmp / "home")
from bytecat import db, merge, scan

try:
    print("\n[Linux: filesystem UUID from mountinfo and by-uuid]")
    drive = tmp / "usb drive"
    drive.mkdir()
    mp = scan.mount_point(str(drive))
    dev = os.stat(mp).st_dev
    (tmp / "dev").mkdir()
    (tmp / "dev" / "sdz1").write_text("")
    by_uuid = tmp / "by-uuid"
    by_uuid.mkdir()
    os.symlink("../dev/sdz1", by_uuid / "1234-ABCD")
    os.symlink("../dev/other", by_uuid / "9999-0000")
    esc = mp.replace(" ", "\\040")
    mountinfo = tmp / "mountinfo"
    mountinfo.write_text(
        f"20 1 0:1 / /proc rw - proc proc rw\n"
        f"36 1 {os.major(dev)}:{os.minor(dev)} / {esc} rw,relatime shared:1 - ext4 "
        f"{tmp}/dev/sdz1 rw\n")
    scan.MOUNTINFO, scan.BY_UUID = str(mountinfo), str(by_uuid)
    info = scan.linux_volume_info(mp)
    check("reads the UUID of the mount's source device", info.get("uuid") == "1234-ABCD", str(info))
    check("and its filesystem type", info.get("fs_type") == "ext4", str(info))

    real_system = platform.system
    scan.platform.system = lambda: "Linux"
    try:
        vol = scan.volume_identity(str(drive))
        check("scan identifies the drive by it", vol["uuid"] == "1234-ABCD", str(vol))
        synthetic = f"synthetic:{platform.node()}:{mp}"
        check("and names the ID an older scan gave it", synthetic in vol.get("aliases", []),
              str(vol))
        forced = scan.volume_identity(str(drive), "MY-ID")
        check("--volume-uuid still wins", forced["uuid"] == "MY-ID")
        check("keeping the drive's own ID", forced.get("fs_uuid") == "1234-ABCD", str(forced))

        scan.BY_UUID = str(tmp / "no-such-dir")
        check("no by-uuid entry falls back to the synthetic ID",
              scan.volume_identity(str(drive))["uuid"] == synthetic)
        scan.BY_UUID = str(by_uuid)

        print("\n[a drive catalogued before keeps its history]")
        conn = db.open_catalog()
        host = db.this_host()

        def merge_lines(*recs):
            stream = io.StringIO("".join(json.dumps(r) + "\n" for r in recs))
            return merge.merge_stream(conn, stream)

        f = {"type": "file", "relpath": "a.txt", "name": "a.txt", "ext": "txt",
             "size": 3, "mtime": 1, "inode": 1, "nlink": 1, "symlink": 0}
        merge_lines({"type": "volume", "host": host, "root": str(drive), "uuid": synthetic}, f)
        before = conn.execute("SELECT id FROM volumes").fetchall()
        check("a mounted drive with only its old ID reads online",
              db.volume_states(conn)[before[0][0]] == "online")
        stats = merge_lines({"type": "volume", "host": host, "root": str(drive), **vol}, f)
        after = conn.execute("SELECT id, uuid FROM volumes").fetchall()
        check("rescanning with the UUID renames the drive, not a second one",
              len(after) == 1 and after[0]["uuid"] == "1234-ABCD", str([tuple(r) for r in after]))
        check("its files are not marked missing", stats["missing"] == 0, str(stats))
        conn.close()
    finally:
        scan.platform.system = real_system

    print("\n[--volume-uuid on a drive mounted here reads online]")
    src = tmp / "forced"
    src.mkdir()
    (src / "f.txt").write_text("x\n")
    env = dict(os.environ)
    s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(src), "--volume-uuid",
                        "FORCED-1"], cwd=ROOT, env=env, capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                   input=s.stdout, capture_output=True, text=True)
    conn = db.connect()
    vid = conn.execute("SELECT id FROM volumes WHERE uuid = 'FORCED-1'").fetchone()[0]
    check("online while mounted", db.volume_states(conn)[vid] == "online")
    src.rename(tmp / "forced-unplugged")
    check("offline once gone", db.volume_states(conn)[vid] == "offline")
    conn.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
