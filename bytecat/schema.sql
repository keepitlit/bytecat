-- bytecat catalog schema
--
-- Identity model: a FILE is (host, volume_uuid, relpath). A BLOB is content,
-- identified by hash. Many files may point at one blob -- that fan-out IS the
-- duplicate report. Volumes are tracked by UUID, never by mount point, so a
-- drive remounting at "/Volumes/Backup 1" does not create phantom duplicates.
--
-- Nothing here stores file bodies. Extracted text is held only long enough to
-- embed; what persists is a vector (in Chroma) plus short derived fields.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hosts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,      -- 'laptop', 'nas', 'desktop'
    platform    TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT
);

-- A volume is a physical/logical store. `uuid` is the stable identity; for
-- network shares with no real UUID we synthesise one and persist it in a
-- marker file at the volume root, so the identity survives remounts.
CREATE TABLE IF NOT EXISTS volumes (
    id           INTEGER PRIMARY KEY,
    host_id      INTEGER NOT NULL REFERENCES hosts(id),
    uuid         TEXT NOT NULL,
    label        TEXT,
    fs_type      TEXT,
    total_bytes  INTEGER,
    last_mount   TEXT,                     -- most recent mount point, informational only
    -- 'online' = seen in the last scan. 'offline' = not currently reachable.
    -- Offline is NORMAL (unplugged drive); it must never cascade into deletes.
    state        TEXT NOT NULL DEFAULT 'online'
                 CHECK (state IN ('online', 'offline')),
    last_scan    TEXT,
    UNIQUE (host_id, uuid)
);

CREATE TABLE IF NOT EXISTS blobs (
    id           INTEGER PRIMARY KEY,
    -- Full content hash (BLAKE2b-256, stdlib -- the scanner must run on hosts
    -- where we cannot install anything). NULL until tier-1 promotes it.
    hash         TEXT UNIQUE,
    size_bytes   INTEGER NOT NULL,
    -- Cheap prefilter: hash of head+tail 1MiB. Files sharing (size, quick_hash)
    -- are the only ones that ever get read in full.
    quick_hash   TEXT,
    n_files      INTEGER NOT NULL DEFAULT 0,   -- denormalised fan-out, maintained by hash.py
    first_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_blobs_quick  ON blobs(size_bytes, quick_hash);
CREATE INDEX IF NOT EXISTS idx_blobs_fanout ON blobs(n_files) WHERE n_files > 1;

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    volume_id   INTEGER NOT NULL REFERENCES volumes(id),
    relpath     TEXT NOT NULL,             -- path relative to the volume root
    name        TEXT NOT NULL,
    ext         TEXT,                      -- lowercased, no dot
    size_bytes  INTEGER NOT NULL,
    mtime       INTEGER NOT NULL,          -- unix seconds
    inode       INTEGER,
    nlink       INTEGER,                   -- >1 means hardlink; not a real duplicate
    is_symlink  INTEGER NOT NULL DEFAULT 0,
    blob_id     INTEGER REFERENCES blobs(id),
    -- Tier progress. Each tier is independently resumable: we resume by asking
    -- "which rows are behind?" rather than by keeping a cursor.
    t1_hashed   INTEGER NOT NULL DEFAULT 0,
    t2_meta     INTEGER NOT NULL DEFAULT 0,
    t3_embedded INTEGER NOT NULL DEFAULT 0,
    t4_enriched INTEGER NOT NULL DEFAULT 0,
    -- 'present' = seen in the most recent scan of its volume.
    -- 'missing' = volume was scanned and this path was NOT found (deleted/moved).
    -- A file on an OFFLINE volume stays 'present' -- we simply have no news.
    state       TEXT NOT NULL DEFAULT 'present'
                CHECK (state IN ('present', 'missing')),
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT,
    -- Which merge run last saw this path. Deletion detection compares against
    -- this rather than a timestamp: datetime('now') has one-second resolution,
    -- so two scans inside the same second made deletions invisible.
    last_run    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (volume_id, relpath)
);

CREATE INDEX IF NOT EXISTS idx_files_blob   ON files(blob_id);
CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_t1     ON files(t1_hashed) WHERE t1_hashed = 0;
CREATE INDEX IF NOT EXISTS idx_files_t2     ON files(t2_meta)   WHERE t2_meta   = 0;
CREATE INDEX IF NOT EXISTS idx_files_t3     ON files(t3_embedded) WHERE t3_embedded = 0;
CREATE INDEX IF NOT EXISTS idx_files_size   ON files(size_bytes);

-- The rename/move cache. `files` is keyed on (volume_id, relpath), so moving a
-- file creates a NEW row with blob_id NULL even though its content, inode and
-- mtime never changed. This index lets hash.adopt_by_inode() find the old row
-- and inherit its fingerprint instead of re-reading the bytes.
CREATE INDEX IF NOT EXISTS idx_files_inode  ON files(volume_id, inode, mtime, size_bytes);

-- Derived, human-meaningful facts about a file. One row per file that has any.
-- `meta_json` holds tier-2 output (EXIF, media props, document properties).
-- `title`/`gist`/`category` are tier-4 LLM output, or cheap heuristics.
CREATE TABLE IF NOT EXISTS extractions (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    kind         TEXT,                     -- coarse class: document|image|video|audio|archive|code|data|other
    meta_json    TEXT,
    text_chars   INTEGER,                  -- length of extracted text (NOT the text itself)
    text_sha     TEXT,                     -- lets us skip re-embedding unchanged content
    title        TEXT,
    gist         TEXT,                     -- one line, <= ~200 chars
    category     TEXT,
    captured_at  TEXT,                     -- best-effort content date (EXIF DateTimeOriginal etc.)
    confidence   REAL,
    source       TEXT,                     -- 'heuristic' | 'llm' | 'exif'
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_extractions_kind ON extractions(kind);
CREATE INDEX IF NOT EXISTS idx_extractions_date ON extractions(captured_at);

-- Keyword half of the hybrid search. Vectors live in Chroma; this is the
-- lexical complement, and it means search still works with Chroma down.
-- Contentless (the text lives in `files`/`extractions`, no second copy) but with
-- contentless_delete so a row can be refreshed in place when enrichment adds a
-- title. Requires SQLite >= 3.43; verified 3.53 here.
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    relpath,
    name,
    title,
    gist,
    content='',
    contentless_delete=1
);

-- Append-only record of every pass, so a long run's history is inspectable.
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    stage        TEXT NOT NULL,            -- scan|merge|hash|extract|embed|enrich
    host         TEXT,
    volume_uuid  TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    n_seen       INTEGER DEFAULT 0,
    n_changed    INTEGER DEFAULT 0,
    n_errors     INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'running',   -- running|ok|failed|interrupted
    note         TEXT
);

-- Files that failed a tier, so a bad PDF doesn't wedge a 20-hour pass.
CREATE TABLE IF NOT EXISTS errors (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER REFERENCES files(id) ON DELETE CASCADE,
    stage     TEXT NOT NULL,
    message   TEXT,                        -- exception class + message ONLY, never file content
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_errors_stage ON errors(stage);
