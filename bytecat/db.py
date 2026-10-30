"""SQLite catalog: connection handling, schema init, and upsert helpers.

The catalog is the source of truth. Chroma holds vectors keyed by file id and is
treated as disposable -- everything in it can be rebuilt from here.

Data lives outside any synced folder (see BYTECAT_HOME). A churning multi-GB
SQLite file inside a Syncthing/Dropbox tree would replicate to every peer
continuously, so the default deliberately points at ~/.local/share/bytecat.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Coarse file classes, keyed by extension. Deliberately not exhaustive: anything
# unmatched falls through to 'other' and is still catalogued, just not enriched.
KIND_BY_EXT: dict[str, str] = {}
for _kind, _exts in {
    "document": (
        "pdf doc docx odt rtf txt md markdown pages tex epub mobi azw3 "
        "ppt pptx key odp xls xlsx numbers ods csv tsv"
    ),
    "image": "jpg jpeg png gif heic heif webp tiff tif bmp svg raw cr2 nef arw dng psd ai",
    "video": "mp4 mov m4v avi mkv webm wmv flv mpg mpeg 3gp",
    "audio": "mp3 m4a aac flac wav aiff ogg opus wma",
    "archive": "zip tar gz bz2 xz 7z rar dmg iso pkg",
    "code": (
        "py js ts tsx jsx go rs rb java kt swift c h cpp hpp cs php sh zsh "
        "sql html css scss vue svelte lua pl r m mm gradle"
    ),
    "data": "json yaml yml toml xml plist ndjson parquet db sqlite sqlite3 log",
    # Single logical objects recorded as one catalog entry (see scan.py).
    "bundle": (
        "app framework photoslibrary fcpbundle logicx sparsebundle xcodeproj "
        "xcworkspace lrlibrary lrcat aplibrary imovielibrary tvlibrary "
        "musiclibrary bundle"
    ),
}.items():
    for _e in _exts.split():
        KIND_BY_EXT[_e] = _kind


def data_home() -> Path:
    """Directory holding all catalog state. Keep it out of synced folders."""
    env = os.environ.get("BYTECAT_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".local/share/bytecat"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_home() / "catalog.db"


def report_path(name: str, explicit: str = "") -> Path:
    """Where a report goes: `explicit` if given (--out), else a timestamped
    file under data_home()/reports."""
    if explicit:
        return Path(explicit)
    return data_home() / "reports" / f"{name}-{datetime.now():%Y%m%d-%H%M}.md"


class NoCatalog(RuntimeError):
    """Raised when a read path is asked for a catalog that does not exist yet."""


def connect(path: Optional[Path] = None, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the catalog, applying the pragmas that matter for bulk work.

    `readonly` is used by the search and report paths so that a query can never
    mutate the catalog by accident.
    """
    p = path or db_path()
    if readonly:
        # SQLite cannot open a read-only handle to a file that does not exist,
        # and says so as "unable to open database file" -- which reads like
        # corruption rather than "you have not indexed anything yet".
        if not p.exists():
            raise NoCatalog(
                f"no catalog at {p}; start with: bytecat scan ~/Documents | bytecat merge")
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(p, timeout=60.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        # Bulk merges touch a lot of pages; a bigger cache measurably helps.
        conn.execute("PRAGMA cache_size = -131072")  # ~128 MB
        conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate_columns(conn)
    _migrate_fts(conn)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a catalog was first created."""
    have = {c[1] for c in conn.execute("PRAGMA table_info(files)")}
    if "last_run" not in have:
        conn.execute("ALTER TABLE files ADD COLUMN last_run INTEGER NOT NULL DEFAULT 0")


def _migrate_fts(conn: sqlite3.Connection) -> None:
    """Rebuild files_fts if it predates the contentless_delete option.

    The first cut of the schema created a plain contentless table, which cannot
    DELETE a row -- so refreshing a file's entry after enrichment failed. Catch
    catalogs built with that version and rebuild the index in place.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'files_fts'"
    ).fetchone()
    if row and "contentless_delete" not in (row[0] or ""):
        conn.execute("DROP TABLE files_fts")
        conn.executescript(SCHEMA_PATH.read_text())
        fts_sync(conn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. With isolation_level=None we drive BEGIN ourselves."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# hosts / volumes
# --------------------------------------------------------------------------

def upsert_host(conn: sqlite3.Connection, name: str, platform: str = "") -> int:
    conn.execute(
        """INSERT INTO hosts (name, platform, last_seen)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(name) DO UPDATE SET
               last_seen = datetime('now'),
               platform  = COALESCE(NULLIF(excluded.platform, ''), hosts.platform)""",
        (name, platform),
    )
    return conn.execute("SELECT id FROM hosts WHERE name = ?", (name,)).fetchone()["id"]


def upsert_volume(
    conn: sqlite3.Connection,
    host_id: int,
    uuid: str,
    *,
    label: str = "",
    fs_type: str = "",
    total_bytes: Optional[int] = None,
    mount: str = "",
) -> int:
    conn.execute(
        """INSERT INTO volumes (host_id, uuid, label, fs_type, total_bytes, last_mount,
                                state, last_scan)
           VALUES (?, ?, ?, ?, ?, ?, 'online', datetime('now'))
           ON CONFLICT(host_id, uuid) DO UPDATE SET
               label       = COALESCE(NULLIF(excluded.label, ''), volumes.label),
               fs_type     = COALESCE(NULLIF(excluded.fs_type, ''), volumes.fs_type),
               total_bytes = COALESCE(excluded.total_bytes, volumes.total_bytes),
               last_mount  = COALESCE(NULLIF(excluded.last_mount, ''), volumes.last_mount),
               state       = 'online',
               last_scan   = datetime('now')""",
        (host_id, uuid, label, fs_type, total_bytes, mount),
    )
    return conn.execute(
        "SELECT id FROM volumes WHERE host_id = ? AND uuid = ?", (host_id, uuid)
    ).fetchone()["id"]


def resolve_volume(conn: sqlite3.Connection, value: str) -> list[int]:
    """Volume ids for a value a person typed: UUID, then label, then mount.

    Exact matches only. A subfolder of a mount is not widened to the whole
    drive: coverage compares whole volumes, so that would answer a different
    question. A label two drives share is refused, never guessed. Exits with
    the reason when nothing, or more than one drive, matches.
    """
    rows = conn.execute(
        "SELECT v.id, v.uuid, v.label, v.last_mount, h.name AS host "
        "FROM volumes v JOIN hosts h ON h.id = v.host_id ORDER BY h.name, v.id"
    ).fetchall()
    mount = os.path.abspath(value) if value else ""
    for key, want in (("uuid", value), ("label", value), ("last_mount", mount)):
        hit = [r for r in rows if r[key] and
               (os.path.abspath(r[key]) if key == "last_mount" else r[key]) == want]
        if hit:
            break
    if not hit:
        sys.exit(f"[bytecat] no volume {value!r}; `bytecat status` lists them")
    # The same UUID on two hosts is one drive scanned from both: keep both.
    if key != "uuid" and len(hit) > 1:
        print(f"[bytecat] {value!r} matches {len(hit)} volumes:", file=sys.stderr)
        for r in hit:
            print(f"  {r['host']}  {r['last_mount'] or r['label']}  {r['uuid']}",
                  file=sys.stderr)
        sys.exit("[bytecat] pass the UUID")
    return [r["id"] for r in hit]


def this_host() -> str:
    """The host name scan records by default for drives on this machine."""
    return platform.node().split(".")[0]


def _volume_here(uuid: str, root: str) -> bool:
    """Whether the drive with this identity is mounted at its last scan root.

    Identity is re-derived the way scan derives it, so a different drive
    mounted at the same path reads as offline. A synthetic identity now (no
    UUID, no marker) proves nothing against a forced or marker one, so only a
    real mismatch counts. Known gap: a local drive scanned with --volume-uuid
    reads as offline, because diskutil reports its real UUID.
    """
    if not root or not os.path.isdir(root):
        return False
    from . import scan
    now = scan.volume_identity(root)["uuid"]
    if now == uuid:
        return True
    return now.startswith("synthetic:") and not uuid.startswith("synthetic:")


def volume_states(conn: sqlite3.Connection) -> dict[int, str]:
    """Each volume's state now: this machine's drives are checked on disk;
    another host's drives keep the state their last scan left."""
    here = this_host()
    states: dict[int, str] = {}
    for r in conn.execute("""SELECT v.id, v.uuid, v.last_mount, v.state, h.name AS host
                             FROM volumes v JOIN hosts h ON h.id = v.host_id"""):
        if r["host"] == here:
            states[r["id"]] = "online" if _volume_here(r["uuid"], r["last_mount"]) \
                else "offline"
        else:
            states[r["id"]] = r["state"]
    return states


def refresh_volume_states(conn: sqlite3.Connection) -> None:
    """Store volume_states(), so the tiers skip drives that are unplugged.

    Offline is not an error and never touches the volume's file rows: an
    unplugged drive must not look like 400k deletions. The next merge of the
    drive marks it online again.
    """
    for vid, state in volume_states(conn).items():
        conn.execute("UPDATE volumes SET state = ? WHERE id = ? AND state != ?",
                     (state, vid, state))
    conn.commit()


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

FILE_COLUMNS = (
    "volume_id", "relpath", "name", "ext", "size_bytes",
    "mtime", "inode", "nlink", "is_symlink",
)


def upsert_files(conn: sqlite3.Connection, rows: Iterable[tuple],
                 run_id: int = 0) -> int:
    """Idempotent bulk ingest keyed on (volume_id, relpath).

    Re-running a scan of an unchanged tree must be a no-op. When size or mtime
    changes we reset the tier flags for that row, because whatever we derived
    from the old bytes is now stale.

    `run_id` stamps every row this merge touched, which is what makes deletion
    detection exact -- see mark_missing.
    """
    rows = [(*r, run_id) for r in rows]
    cur = conn.executemany(
        f"""INSERT INTO files ({', '.join(FILE_COLUMNS)}, state, last_seen, last_run)
            VALUES ({', '.join('?' * len(FILE_COLUMNS))}, 'present',
                    datetime('now'), ?)
            ON CONFLICT(volume_id, relpath) DO UPDATE SET
                last_run   = excluded.last_run,
                size_bytes = excluded.size_bytes,
                mtime      = excluded.mtime,
                inode      = excluded.inode,
                nlink      = excluded.nlink,
                is_symlink = excluded.is_symlink,
                state      = 'present',
                last_seen  = datetime('now'),
                -- content changed => every derived tier is invalidated
                blob_id     = CASE WHEN excluded.size_bytes != files.size_bytes
                                     OR excluded.mtime      != files.mtime
                                   THEN NULL ELSE files.blob_id END,
                t1_hashed   = CASE WHEN excluded.size_bytes != files.size_bytes
                                     OR excluded.mtime      != files.mtime
                                   THEN 0 ELSE files.t1_hashed END,
                t2_meta     = CASE WHEN excluded.size_bytes != files.size_bytes
                                     OR excluded.mtime      != files.mtime
                                   THEN 0 ELSE files.t2_meta END,
                t3_embedded = CASE WHEN excluded.size_bytes != files.size_bytes
                                     OR excluded.mtime      != files.mtime
                                   THEN 0 ELSE files.t3_embedded END,
                t4_enriched = CASE WHEN excluded.size_bytes != files.size_bytes
                                     OR excluded.mtime      != files.mtime
                                   THEN 0 ELSE files.t4_enriched END""",
        rows,
    )
    return cur.rowcount


def mark_missing(conn: sqlite3.Connection, volume_id: int, run_id: int) -> int:
    """Flag rows this merge run did not see, on a volume it actually scanned.

    Compares run ids rather than timestamps: datetime('now') is second-
    resolution, so a rescan finishing inside the same second as the previous one
    left deleted files looking present.

    Rows are marked, never deleted -- a file that comes back keeps its history,
    and a mistaken sweep stays recoverable.
    """
    cur = conn.execute(
        """UPDATE files SET state = 'missing'
           WHERE volume_id = ? AND state = 'present' AND last_run != ?""",
        (volume_id, run_id),
    )
    return cur.rowcount


def kind_for_ext(ext: str) -> str:
    return KIND_BY_EXT.get((ext or "").lower(), "other")


# --------------------------------------------------------------------------
# which files a tier may open
# --------------------------------------------------------------------------

def behind(tier_col: str) -> str:
    """WHERE clause, over `files f JOIN volumes v`, for files still behind on a
    tier that the tier may open now: present, not a symlink, on a volume that
    is online. An offline drive is skipped, never treated as deleted."""
    return (f"f.{tier_col} = 0 AND f.state = 'present' "
            "AND f.is_symlink = 0 AND v.state = 'online'")


def on_disk(rows: Iterable[sqlite3.Row]) -> Iterator[tuple[sqlite3.Row, str]]:
    """(row, path) for each row whose file exists at its volume's last mount.

    Rows need `last_mount` and `relpath`; a row with no mount is skipped.
    """
    for r in rows:
        mount = r["last_mount"]
        if not mount:
            continue
        p = os.path.join(mount, r["relpath"])
        if os.path.exists(p):
            yield r, p


# --------------------------------------------------------------------------
# FTS
# --------------------------------------------------------------------------

def fts_sync(conn: sqlite3.Connection, file_ids: Optional[list[int]] = None) -> int:
    """Rebuild FTS rows for the given files (or every present file).

    files_fts is contentless and keyed by rowid = files.id, so a delete+insert
    pair is the correct way to refresh a row.
    """
    where = "f.state = 'present'"
    params: list[Any] = []
    if file_ids:
        where += f" AND f.id IN ({','.join('?' * len(file_ids))})"
        params = list(file_ids)

    rows = conn.execute(
        f"""SELECT f.id, f.relpath, f.name,
                   COALESCE(e.title, ''), COALESCE(e.gist, '')
            FROM files f LEFT JOIN extractions e ON e.file_id = f.id
            WHERE {where}""",
        params,
    ).fetchall()

    n = 0
    for r in rows:
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (r[0],))
        conn.execute(
            "INSERT INTO files_fts (rowid, relpath, name, title, gist) VALUES (?,?,?,?,?)",
            (r[0], r[1], r[2], r[3], r[4]),
        )
        n += 1
    return n


# --------------------------------------------------------------------------
# runs / errors
# --------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, stage: str, *, host: str = "",
              volume_uuid: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO runs (stage, host, volume_uuid) VALUES (?,?,?)",
        (stage, host, volume_uuid),
    )
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str = "ok",
               n_seen: int = 0, n_changed: int = 0, n_errors: int = 0,
               note: str = "") -> None:
    conn.execute(
        """UPDATE runs SET finished_at = datetime('now'), status = ?,
                           n_seen = ?, n_changed = ?, n_errors = ?, note = ?
           WHERE id = ?""",
        (status, n_seen, n_changed, n_errors, note, run_id),
    )


def log_error(conn: sqlite3.Connection, file_id: Optional[int], stage: str,
              exc: BaseException) -> None:
    """Record a failure WITHOUT ever persisting file content.

    Only the exception class and its message are stored, and the message is
    truncated -- some libraries helpfully embed the offending bytes.
    """
    msg = f"{type(exc).__name__}: {exc}"[:500]
    conn.execute(
        "INSERT INTO errors (file_id, stage, message) VALUES (?,?,?)",
        (file_id, stage, msg),
    )


def open_catalog() -> sqlite3.Connection:
    """Convenience: open + ensure schema. Safe to call repeatedly."""
    conn = connect()
    init_db(conn)
    return conn
