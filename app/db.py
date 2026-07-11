import sqlite3
from pathlib import Path

from app.config import get_settings

# Tables first (idempotent), then ALTER migrations for new columns, then
# indexes/triggers — because some indexes reference columns (verify_token) that
# only the migration adds to a pre-existing table.
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS sightings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL DEFAULT 'site',
  reddit_username TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sighted_at TEXT NOT NULL,
  tz_name TEXT NOT NULL DEFAULT 'UTC',
  duration_seconds INTEGER,
  shape TEXT,
  witnesses INTEGER,
  num_objects TEXT,
  distance TEXT,
  apparent_size TEXT,
  movement TEXT,
  has_wings TEXT,
  has_rotors TEXT,
  has_plume TEXT,
  makes_noise TEXT,
  sensors TEXT,
  witness_background TEXT,
  location_text TEXT NOT NULL DEFAULT '',
  city TEXT,
  country TEXT,
  lat REAL,
  lon REAL,
  location_obscured INTEGER NOT NULL DEFAULT 0,
  submitter_ip TEXT,
  username_verified INTEGER NOT NULL DEFAULT 0,
  verify_token TEXT,
  verify_sent_at TEXT,
  reddit_post_id TEXT UNIQUE,
  reddit_score INTEGER NOT NULL DEFAULT 0,
  reddit_num_comments INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending_post',
  featured INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sighting_id INTEGER NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
  r2_key TEXT NOT NULL,
  thumb_key TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('image','video')),
  width INTEGER,
  height INTEGER,
  duration REAL,
  size_bytes INTEGER,
  sort_order INTEGER NOT NULL DEFAULT 0,
  thumb_attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  access_token TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
  username TEXT PRIMARY KEY,
  form_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS rate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ip TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS sightings_fts USING fts5(
  title, description, location_text,
  content='sightings', content_rowid='id'
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sightings_status_created ON sightings(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sightings_shape ON sightings(shape);
CREATE INDEX IF NOT EXISTS idx_sightings_country ON sightings(country);
CREATE INDEX IF NOT EXISTS idx_sightings_verify_token ON sightings(verify_token);
CREATE INDEX IF NOT EXISTS idx_media_sighting ON media(sighting_id);
CREATE INDEX IF NOT EXISTS idx_rate_events_lookup ON rate_events(ip, action, created_at);

CREATE TRIGGER IF NOT EXISTS sightings_fts_ai AFTER INSERT ON sightings BEGIN
  INSERT INTO sightings_fts(rowid, title, description, location_text)
  VALUES (new.id, new.title, new.description, new.location_text);
END;
CREATE TRIGGER IF NOT EXISTS sightings_fts_ad AFTER DELETE ON sightings BEGIN
  INSERT INTO sightings_fts(sightings_fts, rowid, title, description, location_text)
  VALUES ('delete', old.id, old.title, old.description, old.location_text);
END;
CREATE TRIGGER IF NOT EXISTS sightings_fts_au AFTER UPDATE OF title, description, location_text ON sightings BEGIN
  INSERT INTO sightings_fts(sightings_fts, rowid, title, description, location_text)
  VALUES ('delete', old.id, old.title, old.description, old.location_text);
  INSERT INTO sightings_fts(rowid, title, description, location_text)
  VALUES (new.id, new.title, new.description, new.location_text);
END;
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI resolves sync dependencies in threadpool
    # threads while async handlers run on the event loop — each per-request
    # connection is still used by only one request at a time.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_MIGRATION_COLUMNS = [
    ("submitter_ip", "TEXT"),
    ("username_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("verify_token", "TEXT"),
    ("verify_sent_at", "TEXT"),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_TABLES)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE sightings ADD COLUMN {name} {decl}")
    conn.executescript(SCHEMA_INDEXES)
    conn.commit()


def get_db():
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
