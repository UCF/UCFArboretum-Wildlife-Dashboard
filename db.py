"""
SQLite storage for the app's metadata (everything that used to live in the
JSON files under runs/).

How this module is meant to be used
-----------------------------------
app.py keeps its in-memory dictionaries (videos, jobs, locations, ...) as a
fast read cache, exactly as before. What changes is how changes reach disk:
instead of rewriting a whole JSON file, the code writes just the record that
changed, with upsert() / delete(). Each write is a SQLite transaction, so it
either fully happens or doesn't happen at all — a crash or full disk mid-
write can no longer leave a half-written file behind.

    import db
    db.init_db()                                  # once, at startup
    videos = db.load_all("videos")                # {id: {...}, ...}
    db.upsert("videos", videos[video_id])         # after changing one record
    db.delete("videos", video_id)

Quick SQLite vocabulary
-----------------------
- table:       like a spreadsheet tab; one row per record.
- column:      one field of a record, with a type (TEXT, INTEGER, REAL).
- primary key: the column that uniquely identifies a row (e.g. videos.id).
- index:       a lookup structure that makes searching by a column fast.
- transaction: a group of writes that is saved all-or-nothing.

SQLite has no true/false or list types, so this module converts at the
boundary: booleans are stored as 0/1, and the few list/dict-valued fields
(a job's command line, an OCR preset's box, ...) are stored as JSON text.
COLUMNS below records which fields need which conversion, and is also the
single list of fields each table accepts — upsert() refuses unknown keys
rather than silently dropping them.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "runs" / "wildlife.db"

# Bump this (and add a step in _upgrade_schema) whenever the schema changes
# after release. Stored in the database file itself via PRAGMA user_version.
SCHEMA_VERSION = 1

SCHEMA = """
-- One row per media file (video or photo) in the Library.
CREATE TABLE IF NOT EXISTS videos (
    id                  TEXT PRIMARY KEY,   -- sha1(job_id:filename)[:16], see video_id_for
    job_id              TEXT NOT NULL,
    folder              TEXT NOT NULL,
    filename            TEXT NOT NULL,
    display_filename    TEXT,
    media_type          TEXT NOT NULL DEFAULT 'video',  -- 'video' | 'photo'

    -- AI output (never edited by people)
    ai_species          TEXT,               -- NULL means the AI said blank
    ai_classifier_conf  REAL,
    ai_detector_conf    REAL,

    -- Human correction (wins over ai_species when set)
    corrected_species   TEXT,
    corrected_at        TEXT,               -- ISO timestamp
    corrected_by        TEXT,               -- "First Last"

    -- Flags (0/1)
    favorited           INTEGER NOT NULL DEFAULT 0,
    marked_for_review   INTEGER NOT NULL DEFAULT 1,
    metadata_edited     INTEGER NOT NULL DEFAULT 0,
    has_bar_crop        INTEGER NOT NULL DEFAULT 0,
    has_thumbnail       INTEGER NOT NULL DEFAULT 0,

    -- OCR / user-editable metadata
    date                TEXT,
    time                TEXT,
    location            TEXT,
    temperature         TEXT,               -- as read off the info bar, e.g. "72°F"
    diel_period         TEXT,               -- Day | Night | Dawn | Dusk
    count               INTEGER NOT NULL DEFAULT 0,
    notes               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_videos_job_id   ON videos(job_id);
CREATE INDEX IF NOT EXISTS idx_videos_location ON videos(location);
CREATE INDEX IF NOT EXISTS idx_videos_date     ON videos(date);

-- One row per processing job (one submitted upload folder).
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    folder              TEXT NOT NULL,
    batch_id            TEXT,
    display_name        TEXT,
    ocr_config          TEXT,               -- OCR preset name, or the skip sentinel
    location            TEXT,
    status              TEXT NOT NULL,      -- queued | running | cancelling | done | failed | cancelled
    seq                 INTEGER,            -- queue order
    started_at          TEXT,
    started_running_at  TEXT,
    finished_at         TEXT,
    output_json         TEXT,
    log_file            TEXT,
    cmd                 TEXT                -- JSON list of command-line arguments
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- One row per in-progress or finished browser upload.
CREATE TABLE IF NOT EXISTS upload_batches (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL,      -- uploading | processing | ...
    created_at          TEXT,
    updated_at          TEXT
);

-- The files inside an upload batch, with resumable-upload chunk tracking.
-- Deleting a batch deletes its file rows too (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS upload_batch_files (
    batch_id            TEXT NOT NULL REFERENCES upload_batches(id) ON DELETE CASCADE,
    rel_path            TEXT NOT NULL,
    size                INTEGER NOT NULL,
    expected_chunks     INTEGER NOT NULL,
    received_chunks     TEXT NOT NULL DEFAULT '[]',  -- JSON list of chunk indexes
    PRIMARY KEY (batch_id, rel_path)
);

-- Curated camera sites (Settings -> Locations).
CREATE TABLE IF NOT EXISTS locations (
    name                TEXT PRIMARY KEY,
    lat                 REAL NOT NULL,
    lon                 REAL NOT NULL
);

-- The full taxonomy SpeciesNet can output, learned from job results.
CREATE TABLE IF NOT EXISTS species (
    label               TEXT PRIMARY KEY
);

-- Per-species-group display settings (privacy, custom cover image).
CREATE TABLE IF NOT EXISTS categories (
    label               TEXT PRIMARY KEY,
    private             INTEGER NOT NULL DEFAULT 0,
    cover               TEXT,               -- filename under the covers dir
    cover_version       INTEGER NOT NULL DEFAULT 0
);

-- Saved OCR presets (where the info bar sits in the frame).
CREATE TABLE IF NOT EXISTS ocr_configs (
    name                TEXT PRIMARY KEY,
    bar_box             TEXT,               -- JSON [x1, y1, x2, y2]
    ref_width           INTEGER,
    ref_height          INTEGER
);

-- Sign-in accounts (managed with manage_users.py).
CREATE TABLE IF NOT EXISTS users (
    username            TEXT PRIMARY KEY,   -- lowercase
    password_hash       TEXT NOT NULL,
    first_name          TEXT NOT NULL DEFAULT '',
    last_name           TEXT NOT NULL DEFAULT ''
);

-- Small one-off settings that don't deserve their own table
-- (storage/auto-empty settings, last-used OCR preset, ...).
CREATE TABLE IF NOT EXISTS settings (
    key                 TEXT PRIMARY KEY,
    value               TEXT                -- JSON
);

-- One-time data migrations already applied (replaces migrations.json).
CREATE TABLE IF NOT EXISTS migrations (
    name                TEXT PRIMARY KEY,
    applied_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Per table: its primary-key column(s), and every column with how its Python
# value is converted ("bool" -> 0/1, "json" -> JSON text, None -> stored as is).
COLUMNS = {
    "videos": {
        "key": ("id",),
        "fields": {
            "id": None, "job_id": None, "folder": None, "filename": None,
            "display_filename": None, "media_type": None,
            "ai_species": None, "ai_classifier_conf": None, "ai_detector_conf": None,
            "corrected_species": None, "corrected_at": None, "corrected_by": None,
            "favorited": "bool", "marked_for_review": "bool", "metadata_edited": "bool",
            "has_bar_crop": "bool", "has_thumbnail": "bool",
            "date": None, "time": None, "location": None, "temperature": None,
            "diel_period": None, "count": None, "notes": None,
        },
    },
    "jobs": {
        "key": ("id",),
        "fields": {
            "id": None, "folder": None, "batch_id": None, "display_name": None,
            "ocr_config": None, "location": None, "status": None, "seq": None,
            "started_at": None, "started_running_at": None, "finished_at": None,
            "output_json": None, "log_file": None, "cmd": "json",
        },
    },
    "upload_batches": {
        "key": ("id",),
        "fields": {"id": None, "status": None, "created_at": None, "updated_at": None},
    },
    "upload_batch_files": {
        "key": ("batch_id", "rel_path"),
        "fields": {
            "batch_id": None, "rel_path": None, "size": None,
            "expected_chunks": None, "received_chunks": "json",
        },
    },
    "locations": {
        "key": ("name",),
        "fields": {"name": None, "lat": None, "lon": None},
    },
    "species": {
        "key": ("label",),
        "fields": {"label": None},
    },
    "categories": {
        "key": ("label",),
        "fields": {"label": None, "private": "bool", "cover": None, "cover_version": None},
    },
    "ocr_configs": {
        "key": ("name",),
        "fields": {"name": None, "bar_box": "json", "ref_width": None, "ref_height": None},
    },
    "users": {
        "key": ("username",),
        "fields": {"username": None, "password_hash": None, "first_name": None, "last_name": None},
    },
}


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------
# A sqlite3 connection must not be shared between threads, and the server
# runs 8 request threads plus the job worker. So each thread lazily opens its
# own connection and reuses it for the life of the thread.

_local = threading.local()


def _open(path):
    conn = sqlite3.connect(
        path,
        timeout=30,              # wait up to 30s for another thread's write to finish
        isolation_level=None,    # we manage transactions ourselves (see transaction())
        check_same_thread=True,
    )
    conn.row_factory = sqlite3.Row
    # WAL ("write-ahead log") mode lets readers keep reading while a write is
    # in progress, and is the standard choice for a multi-threaded server.
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL is safe in WAL mode (no corruption on power loss) and much faster
    # than FULL; at worst the very last committed write is lost on power cut.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect():
    """This thread's connection to DB_PATH, opened on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != DB_PATH:
        if conn is not None:
            conn.close()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(DB_PATH)
        _local.conn, _local.path = conn, DB_PATH
    return conn


def close():
    """Closes this thread's connection (tests use this; the server needn't)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def transaction():
    """
    Groups several writes so they're saved all-or-nothing:

        with db.transaction() as conn:
            db.upsert("videos", a, conn=conn)
            db.upsert("videos", b, conn=conn)

    BEGIN IMMEDIATE takes the write lock up front, so two threads can't both
    start a transaction and then deadlock trying to upgrade to writing.
    Nested use joins the outer transaction.
    """
    conn = connect()
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db():
    """Creates any missing tables/indexes and upgrades older schemas. Safe to call every startup."""
    conn = connect()
    with transaction():
        for statement in _split_sql(SCHEMA):
            conn.execute(statement)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        _upgrade_schema(conn, version)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _upgrade_schema(conn, from_version):
    """
    Future schema changes go here, one `if from_version < N:` block each
    (e.g. ALTER TABLE videos ADD COLUMN ...). Version 0 means a brand-new
    file, which SCHEMA above has already fully created.
    """
    if from_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"{DB_PATH} is schema version {from_version}, newer than this code "
            f"({SCHEMA_VERSION}). Update the app before running it against this database."
        )


def _split_sql(script):
    """
    Splits SCHEMA into individual statements. (sqlite3's executescript()
    would do this, but it forces a COMMIT first, which would break running
    the schema inside our transaction.)
    """
    lines = [line.split("--", 1)[0] for line in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


# --------------------------------------------------------------------------
# Converting between Python dicts and table rows
# --------------------------------------------------------------------------

def _to_db(kind, value):
    if value is None:
        return None
    if kind == "bool":
        return 1 if value else 0
    if kind == "json":
        return json.dumps(value)
    return value


def _from_db(kind, value):
    if value is None:
        return None
    if kind == "bool":
        return bool(value)
    if kind == "json":
        return json.loads(value)
    return value


def row_to_dict(table, row):
    fields = COLUMNS[table]["fields"]
    return {name: _from_db(fields[name], row[name]) for name in row.keys()}


def _key_value(table, record):
    key_cols = COLUMNS[table]["key"]
    return record[key_cols[0]] if len(key_cols) == 1 else tuple(record[c] for c in key_cols)


# --------------------------------------------------------------------------
# Reading and writing records
# --------------------------------------------------------------------------

def upsert(table, record, conn=None):
    """
    Inserts the record, or replaces the existing row with the same primary
    key. `record` is a plain dict using the same keys app.py already uses.
    Keys it leaves out get the column's default. Unknown keys raise
    ValueError, so a new field can't be silently lost — add a column for it.
    """
    fields = COLUMNS[table]["fields"]
    unknown = set(record) - set(fields)
    if unknown:
        raise ValueError(f"{table}: no column for {sorted(unknown)} — add it to db.SCHEMA and db.COLUMNS")
    cols = list(record)
    placeholders = ", ".join("?" for _ in cols)
    key_cols = COLUMNS[table]["key"]
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key_cols)
    conflict = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key_cols)}) {conflict}"
    )
    values = [_to_db(fields[c], record[c]) for c in cols]
    with transaction() if conn is None else _nullcontext(conn) as c:
        c.execute(sql, values)


def upsert_many(table, records):
    """upsert() for a batch of records, saved in a single transaction."""
    with transaction() as conn:
        for record in records:
            upsert(table, record, conn=conn)


def delete(table, key, conn=None):
    """Deletes one row by primary key (a tuple for multi-column keys). Returns True if a row was removed."""
    key_cols = COLUMNS[table]["key"]
    key = key if isinstance(key, tuple) else (key,)
    where = " AND ".join(f"{c} = ?" for c in key_cols)
    with transaction() if conn is None else _nullcontext(conn) as c:
        cur = c.execute(f"DELETE FROM {table} WHERE {where}", key)
    return cur.rowcount > 0


def get(table, key):
    """One record as a dict, or None."""
    key_cols = COLUMNS[table]["key"]
    key = key if isinstance(key, tuple) else (key,)
    where = " AND ".join(f"{c} = ?" for c in key_cols)
    row = connect().execute(f"SELECT * FROM {table} WHERE {where}", key).fetchone()
    return row_to_dict(table, row) if row else None


def load_all(table):
    """Every record in a table as {primary key: dict} — the shape app.py's caches use."""
    rows = connect().execute(f"SELECT * FROM {table}").fetchall()
    out = {}
    for row in rows:
        record = row_to_dict(table, row)
        out[_key_value(table, record)] = record
    return out


def count(table):
    return connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# Settings: small JSON values stored by name.

def get_setting(key, default=None):
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key, value, conn=None):
    with transaction() if conn is None else _nullcontext(conn) as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


# Migrations: names of one-time data fixes already applied.

def applied_migrations():
    return {r["name"] for r in connect().execute("SELECT name FROM migrations")}


def mark_migration_applied(name, conn=None):
    with transaction() if conn is None else _nullcontext(conn) as c:
        c.execute("INSERT OR IGNORE INTO migrations (name) VALUES (?)", (name,))


@contextmanager
def _nullcontext(conn):
    yield conn
