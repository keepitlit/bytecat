#!/usr/bin/env python3
"""How a drive is identified, and recognised again when it is mounted here.

- On Linux the filesystem UUID is read from /proc/self/mountinfo and
  /dev/disk/by-uuid (faked here, so this runs on any OS), and a drive
  catalogued before that under a marker or synthetic ID keeps its history.
- A drive scanned with --volume-uuid on this machine reads online while it
  is mounted, though the drive itself reports a different ID.
"""
import contextlib, io, json, os, platform, shutil, subprocess, sys, tempfile
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
    real_tables = scan.MOUNTINFO, scan.BY_UUID
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
        scan.MOUNTINFO, scan.BY_UUID = real_tables

    print("\n[a network share keeps its ID wherever it is mounted]")
    # mount_point is faked so each folder reads as its own mount.
    real_mp, real_system, real_diskutil = (scan.mount_point, platform.system,
                                           scan.macos_volume_info)
    real_tables = scan.MOUNTINFO, scan.BY_UUID, getattr(scan, "MOUNT_CMD", None)
    try:
        scan.mount_point = lambda p: os.path.abspath(p)
        scan.BY_UUID = str(tmp / "no-such-dir")
        here, there = tmp / "mnt" / "photos", tmp / "media" / "Photos 2"
        here.mkdir(parents=True)
        there.mkdir(parents=True)

        def linux_share(mp, line_src="//NAS.local/Photos", fs="cifs"):
            (tmp / "mi").write_text(
                f"40 1 0:50 / {str(mp).replace(' ', chr(92) + '040')} rw - {fs} "
                f"{line_src} rw,vers=3.0\n")
            scan.MOUNTINFO = str(tmp / "mi")
            return scan.volume_identity(str(mp))

        scan.platform.system = lambda: "Linux"
        a = linux_share(here)
        b = linux_share(there, "//alex@nas.local/photos/")
        check("a CIFS share remounted at another path keeps its ID",
              a["uuid"] == b["uuid"] == "share://nas.local/photos", f"{a} {b}")
        check("and names the synthetic ID an older scan gave it",
              f"synthetic:{platform.node()}:{here}" in a.get("aliases", []), str(a))
        n1 = linux_share(here, "nas:/export/media/", "nfs4")
        n2 = linux_share(there, "nas:/export/media", "nfs4")
        check("an NFS export keeps its ID too",
              n1["uuid"] == n2["uuid"] == "share:nas:/export/media", f"{n1} {n2}")

        scan.platform.system = lambda: "Darwin"
        scan.macos_volume_info = lambda mp: {}  # a share has no disk UUID
        table = (f"/dev/disk3s1 on / (apfs, local)\n"
                 f"//alex@NAS.local/Photos on {there} (smbfs, nodev, nosuid)\n")
        scan.MOUNT_CMD = [sys.executable, "-c", f"print({table!r}, end='')"]
        m = scan.volume_identity(str(there))
        check("macOS reads the share from mount",
              m["uuid"] == "share://nas.local/photos", str(m))

        print("\n[a drive with no stable ID says so]")
        scan.platform.system = lambda: "Linux"
        (tmp / "mi").write_text("")
        scan.MOUNTINFO = str(tmp / "mi")
        err, out = io.StringIO(), io.StringIO()
        real_argv = sys.argv
        sys.argv = ["scan.py", str(here)]
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                scan.main()
        finally:
            sys.argv = real_argv
        check("the synthetic fallback names --volume-uuid and --allow-marker",
              "--volume-uuid" in err.getvalue() and "--allow-marker" in err.getvalue(),
              err.getvalue())
    finally:
        scan.mount_point, scan.platform.system = real_mp, real_system
        scan.macos_volume_info = real_diskutil
        scan.MOUNTINFO, scan.BY_UUID, scan.MOUNT_CMD = real_tables

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
