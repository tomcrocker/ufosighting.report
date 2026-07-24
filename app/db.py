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

-- Every key we hand out a presigned upload URL for. A file the browser
-- uploads but never attaches to a submission leaves no other trace, so this
-- is what makes orphaned uploads findable (see app/orphans.py).
-- Runtime, admin-toggleable settings (e.g. the moderation hold). Kept in the
-- DB, not env, so a moderator can flip them from /admin with no restart.
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS upload_keys (
  key TEXT PRIMARY KEY,
  ip TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS geocode_cache (
  query TEXT PRIMARY KEY,
  lat REAL,
  lon REAL,
  city TEXT,
  country TEXT,
  display_name TEXT,
  cached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS comments (
  reddit_comment_id TEXT PRIMARY KEY,
  sighting_id INTEGER NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  created_utc INTEGER NOT NULL DEFAULT 0,
  permalink TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS yt_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sighting_id INTEGER NOT NULL UNIQUE REFERENCES sightings(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS analytics_visits (
  day TEXT NOT NULL,
  visitor TEXT NOT NULL,          -- daily-salted IP hash, not reversible
  hits INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (day, visitor)      -- dedup + indexes the day prefix
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
CREATE INDEX IF NOT EXISTS idx_yt_jobs_status ON yt_jobs(status);
CREATE INDEX IF NOT EXISTS idx_comments_sighting ON comments(sighting_id, score DESC);

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
    ("rule_out", "TEXT"),
    ("capture_device", "TEXT"),
    ("obs_accel", "TEXT"),
    ("obs_no_signature", "TEXT"),
    ("obs_low_observability", "TEXT"),
    ("obs_transmedium", "TEXT"),
    ("obs_positive_lift", "TEXT"),
    ("sky_events", "TEXT"),
    ("reddit_posted_at", "TEXT"),
    ("removed_by_category", "TEXT"),  # raw Reddit removal reason (moderator/reddit/…)
    ("bsky_posted_at", "TEXT"),       # NULL=eligible, ISO ts=posted, 'skipped'=pre-rollout
    ("bsky_uri", "TEXT"),             # at:// URI of the Bluesky post, for retraction
    ("first_hand", "INTEGER NOT NULL DEFAULT 1"),  # 0 = shared second-hand report
    ("source_note", "TEXT"),          # where a shared (second-hand) sighting came from
    ("bot_comment_id", "TEXT"),       # bot's pinned details comment, so the sky
                                      # worker can edit computed passes into it
    # Deferred posting: the verify click queues the sighting instead of posting
    # inline, so media processing (video posters especially) can finish first.
    ("pending_post_at", "TEXT"),      # when it entered the post queue
    ("post_attempts", "INTEGER NOT NULL DEFAULT 0"),
    # Reddit posts can't mix video and photos, so when a reporter uploads both
    # they pick which one leads: 'video' | 'images' (NULL = video-first default)
    ("primary_media", "TEXT"),
    # why a sighting was routed to the review queue (CQS-proxy gate, ban,
    # moderation hold) — shown to the moderator on the review card
    ("review_reason", "TEXT"),
    # JSON account intel (age, karma/yr, dormancy, subreddits, AI summary)
    # captured at verify time, rendered in the review panel
    ("account_intel", "TEXT"),
    # reporter's required explanation when their first-hand media doesn't look
    # like an original camera file (screenshot / edited / stripped metadata)
    ("media_note", "TEXT"),
    # the reporter's own title, kept for reference — `title` holds the
    # AI-standardized version actually posted (see app/titlegen.py)
    ("original_title", "TEXT"),
    # 1 when mods re-flaired the Reddit post to "Likely Identified" (synced)
    ("likely_identified", "INTEGER NOT NULL DEFAULT 0"),
]


_MEDIA_MIGRATION_COLUMNS = [
    ("exif_json", "TEXT"),
    ("display_key", "TEXT"),
    ("exif_prefs", "TEXT"),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_TABLES)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE sightings ADD COLUMN {name} {decl}")
    existing_media = {r["name"] for r in conn.execute("PRAGMA table_info(media)")}
    for name, decl in _MEDIA_MIGRATION_COLUMNS:
        if name not in existing_media:
            conn.execute(f"ALTER TABLE media ADD COLUMN {name} {decl}")
    conn.executescript(SCHEMA_INDEXES)
    conn.commit()


def get_db():
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
