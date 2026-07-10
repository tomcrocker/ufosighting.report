# ufosighting.report Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build ufosighting.report Phase 1 — structured UFO sighting submission (Reddit OAuth, media direct-to-R2, post-as-user to r/UFOs) plus a public gallery (grid, map, filters, search) with Reddit-mirroring moderation sync.

**Architecture:** FastAPI + Jinja2 SSR + SQLite(WAL, FTS5) on the Oracle VM behind nginx and an existing Cloudflare Tunnel. Browsers upload media directly to Cloudflare R2 via presigned PUT URLs (the VM never proxies media bytes); the app posts sightings to Reddit as the submitting user with their temporary OAuth token; systemd timers run a moderation sync and an orphan cleanup.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, Jinja2, httpx, boto3 (R2 presigning), Pillow + ffmpeg (thumbnails), SQLite FTS5, Leaflet + OpenStreetMap, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-07-10-ufosighting-report-design.md`

## Global Constraints

- Python 3.12 (matches the VM). All Python deps in `requirements.txt`; ffmpeg is a system package.
- 1GB-RAM VM: no Node build step, no Meilisearch, thumbnail jobs run one at a time.
- Media bytes NEVER pass through the app server — presigned PUT direct to R2, serving via `media.ufosighting.report`.
- OAuth tokens are server-side only (sessions table), `duration=temporary`, never stored in cookies, never persisted beyond the ~1h session.
- Limits (from spec): images ≤ 25MB, video ≤ 500MB, max 10 files per sighting.
- App listens on `127.0.0.1:8010` behind nginx `:80`; public via existing cloudflared tunnel.
- `SUBREDDIT` env var — dev and tests use a test subreddit; only the prod `.env` says `UFOs`.
- Every Reddit HTTP call sends `User-Agent: web:report.ufosighting:v1.0 (by /u/tmosh)`.
- Reddit endpoints: `www.reddit.com` for authorize/token, `oauth.reddit.com` for API calls.
- Status values: `pending_post`, `live`, `removed_on_reddit`, `deleted_by_user`, `hidden_by_admin`. Only `live` is publicly visible. `hidden_by_admin` is never auto-changed by sync.
- Never delete sighting data on moderation; only flip `status`.
- Commit at the end of every task. `oracle2.key`, `.env`, `data/` are gitignored and must never be committed.
- Run tests with `.venv/bin/pytest` from the repo root (pytest.ini sets `pythonpath = .`).

## File Structure

```
app/
  __init__.py
  config.py            # Settings dataclass from env (.env via python-dotenv)
  db.py                # connect(), init_db(), get_db dependency, SCHEMA + FTS5 triggers
  auth.py              # sessions table CRUD, csrf_for(), drafts CRUD, Session dataclass
  reddit_oauth.py      # login_url(), exchange_code(), fetch_username() (web app OAuth)
  reddit.py            # submit_post() as user, script_token(), fetch_posts_info(), status mapping
  r2.py                # boto3 client, presign_put(), public_url(), head_exists(), put/delete/list
  helpers.py           # slugify(), humanize_duration(), format_post_body(), SHAPES
  thumbs.py            # thumbnail generation (Pillow/ffmpeg) + background worker thread
  web.py               # Jinja2 templates object, current_user/require_admin dependencies
  main.py              # create_app() factory, lifespan (init_db + thumb worker), static mount
  routes/
    __init__.py
    auth.py            # /auth/login, /auth/callback, /auth/logout
    submit.py          # GET/POST /submit, POST /api/presign
    public.py          # /, /sighting/{id}/{slug}, /map, /api/pins, /search, sitemap, robots
    admin.py           # /admin, POST /admin/sighting/{id}/action
  templates/           # base, index, _cards, detail, map, search, submit, login, admin
static/
  css/site.css
  js/upload.js         # presigned direct-to-R2 uploads with progress + retry
  js/map.js            # Leaflet + markercluster pins
sync.py                # moderation sync entrypoint (systemd timer)
cleanup.py             # orphaned R2 uploads + expired sessions/drafts/pending rows
tests/
  conftest.py, test_config.py, test_db.py, test_r2.py, test_auth.py,
  test_auth_routes.py, test_reddit.py, test_presign.py, test_helpers.py,
  test_submit.py, test_thumbs.py, test_public.py, test_map_search.py,
  test_admin.py, test_sync.py, test_cleanup.py
deploy/
  deploy.sh, nginx-ufosighting.conf, ufosighting-web.service,
  ufosighting-sync.service, ufosighting-sync.timer,
  ufosighting-cleanup.service, ufosighting-cleanup.timer, RUNBOOK.md
requirements.txt, pytest.ini, .env.example
```

---

### Task 1: Project scaffold + config module

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `.env.example`, `app/__init__.py`, `app/routes/__init__.py`, `app/config.py`
- Test: `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `app.config.get_settings() -> Settings` (lru_cached). `Settings` is a frozen dataclass with fields: `base_url, db_path, secret_key, media_base_url, r2_endpoint, r2_bucket, r2_access_key, r2_secret_key, reddit_client_id, reddit_client_secret, reddit_redirect_uri, script_client_id, script_client_secret, script_username, script_password, subreddit, sighting_flair_id, admin_users (tuple[str,...], lowercased), user_agent, session_ttl_seconds (3600), max_image_bytes (26214400), max_video_bytes (524288000), max_files (10)`.
- Produces: test env baseline in `tests/conftest.py` (`TEST_ENV` dict, autouse settings-cache reset, `db_conn` fixture added in Task 2).

- [ ] **Step 1: Create scaffold files**

`requirements.txt`:
```
fastapi>=0.115
uvicorn[standard]>=0.30
jinja2>=3.1
httpx>=0.27
boto3>=1.34
Pillow>=10.3
python-dotenv>=1.0
python-multipart>=0.0.9
pytest>=8.2
respx>=0.21
```

`pytest.ini`:
```ini
[pytest]
pythonpath = .
testpaths = tests
```

`.env.example`:
```bash
# Copy to .env and fill in. NEVER commit .env.
BASE_URL=http://localhost:8010
DB_PATH=data/sightings.db
SECRET_KEY=change-me-64-random-chars          # python3 -c "import secrets;print(secrets.token_hex(32))"

# Cloudflare R2 (S3 API)
MEDIA_BASE_URL=https://media.ufosighting.report
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=ufosighting-media
R2_ACCESS_KEY=
R2_SECRET_KEY=

# Reddit WEB app (visitor OAuth: identity+submit) — reddit.com/prefs/apps
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_REDIRECT_URI=http://localhost:8010/auth/callback

# Reddit SCRIPT app (mod account, background sync)
SCRIPT_CLIENT_ID=
SCRIPT_CLIENT_SECRET=
SCRIPT_USERNAME=
SCRIPT_PASSWORD=

# Community
SUBREDDIT=UFOs_sandbox        # test subreddit in dev; UFOs only in prod .env
SIGHTING_FLAIR_ID=            # flair template UUID; empty = don't set flair
ADMIN_USERS=tmosh
```

`app/__init__.py` and `app/routes/__init__.py`: empty files.

- [ ] **Step 2: Create venv and install deps**

```bash
cd /Users/tom/dev/claude/ufos-sightings-website
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
```
Expected: exits 0.

- [ ] **Step 3: Write the failing tests**

`tests/conftest.py`:
```python
import os

TEST_ENV = {
    "BASE_URL": "http://testserver",
    "DB_PATH": "unused-set-per-test.db",
    "SECRET_KEY": "test-secret",
    "MEDIA_BASE_URL": "https://media.test",
    "R2_ENDPOINT": "https://r2.test",
    "R2_BUCKET": "test-bucket",
    "R2_ACCESS_KEY": "AKTEST",
    "R2_SECRET_KEY": "SKTEST",
    "REDDIT_CLIENT_ID": "webapp-id",
    "REDDIT_CLIENT_SECRET": "webapp-secret",
    "REDDIT_REDIRECT_URI": "http://testserver/auth/callback",
    "SCRIPT_CLIENT_ID": "script-id",
    "SCRIPT_CLIENT_SECRET": "script-secret",
    "SCRIPT_USERNAME": "modbot",
    "SCRIPT_PASSWORD": "hunter2",
    "SUBREDDIT": "UFOs_sandbox",
    "SIGHTING_FLAIR_ID": "flair-123",
    "ADMIN_USERS": "tmosh,AdminUser",
}
os.environ.update(TEST_ENV)

import pytest
from app.config import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```

`tests/test_config.py`:
```python
import pytest
from app.config import get_settings


def test_settings_load_from_env():
    s = get_settings()
    assert s.subreddit == "UFOs_sandbox"
    assert s.max_files == 10
    assert s.max_image_bytes == 25 * 1024 * 1024
    assert s.max_video_bytes == 500 * 1024 * 1024
    assert s.user_agent == "web:report.ufosighting:v1.0 (by /u/tmosh)"


def test_admin_users_parsed_lowercase():
    s = get_settings()
    assert s.admin_users == ("tmosh", "adminuser")


def test_missing_required_env_raises(monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        get_settings()
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'app.config'` (or ImportError).

- [ ] **Step 5: Implement `app/config.py`**

```python
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    base_url: str
    db_path: str
    secret_key: str
    media_base_url: str
    r2_endpoint: str
    r2_bucket: str
    r2_access_key: str
    r2_secret_key: str
    reddit_client_id: str
    reddit_client_secret: str
    reddit_redirect_uri: str
    script_client_id: str
    script_client_secret: str
    script_username: str
    script_password: str
    subreddit: str
    sighting_flair_id: str
    admin_users: tuple[str, ...]
    user_agent: str
    session_ttl_seconds: int
    max_image_bytes: int
    max_video_bytes: int
    max_files: int


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@lru_cache
def get_settings() -> Settings:
    return Settings(
        base_url=_env("BASE_URL", "http://localhost:8010").rstrip("/"),
        db_path=_env("DB_PATH", "data/sightings.db"),
        secret_key=_env("SECRET_KEY"),
        media_base_url=_env("MEDIA_BASE_URL").rstrip("/"),
        r2_endpoint=_env("R2_ENDPOINT"),
        r2_bucket=_env("R2_BUCKET"),
        r2_access_key=_env("R2_ACCESS_KEY"),
        r2_secret_key=_env("R2_SECRET_KEY"),
        reddit_client_id=_env("REDDIT_CLIENT_ID"),
        reddit_client_secret=_env("REDDIT_CLIENT_SECRET"),
        reddit_redirect_uri=_env("REDDIT_REDIRECT_URI"),
        script_client_id=_env("SCRIPT_CLIENT_ID", ""),
        script_client_secret=_env("SCRIPT_CLIENT_SECRET", ""),
        script_username=_env("SCRIPT_USERNAME", ""),
        script_password=_env("SCRIPT_PASSWORD", ""),
        subreddit=_env("SUBREDDIT"),
        sighting_flair_id=_env("SIGHTING_FLAIR_ID", ""),
        admin_users=tuple(
            u.strip().lower() for u in _env("ADMIN_USERS", "").split(",") if u.strip()
        ),
        user_agent="web:report.ufosighting:v1.0 (by /u/tmosh)",
        session_ttl_seconds=3600,
        max_image_bytes=25 * 1024 * 1024,
        max_video_bytes=500 * 1024 * 1024,
        max_files=10,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt pytest.ini .env.example app/ tests/
git commit -m "feat: project scaffold and env-based config"
```

---

### Task 2: Database layer (schema, WAL, FTS5 triggers)

**Files:**
- Create: `app/db.py`
- Modify: `tests/conftest.py` (add `db_conn` fixture)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `app.db.connect(db_path: str) -> sqlite3.Connection` (Row factory, WAL, foreign_keys ON, busy_timeout 30s); `app.db.init_db(conn) -> None` (idempotent); `app.db.get_db()` FastAPI dependency yielding a per-request connection to `get_settings().db_path`.
- Produces: tables `sightings`, `media`, `sessions`, `drafts`, FTS5 `sightings_fts` (external-content, trigger-synced). Column names exactly as in the schema below — all later tasks depend on them.

- [ ] **Step 1: Write the failing tests**

Add to `tests/conftest.py` (bottom):
```python
@pytest.fixture
def db_conn(tmp_path):
    from app import db
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_db(conn)
    yield conn
    conn.close()
```

`tests/test_db.py`:
```python
def _insert_sighting(conn, **over):
    row = {
        "reddit_username": "tester", "title": "Bright orb over the lake",
        "description": "A silent orange orb hovered for two minutes.",
        "sighted_at": "2026-07-01T05:00:00Z", "location_text": "Lake Cowichan, BC",
    }
    row.update(over)
    cur = conn.execute(
        """INSERT INTO sightings (reddit_username, title, description, sighted_at, location_text)
           VALUES (:reddit_username, :title, :description, :sighted_at, :location_text)""",
        row,
    )
    conn.commit()
    return cur.lastrowid


def test_init_db_is_idempotent(db_conn):
    from app import db
    db.init_db(db_conn)  # second run must not raise


def test_wal_and_foreign_keys_enabled(db_conn):
    assert db_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_defaults(db_conn):
    sid = _insert_sighting(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_post"
    assert row["source"] == "site"
    assert row["featured"] == 0
    assert row["reddit_score"] == 0


def test_fts_insert_update_delete_stay_in_sync(db_conn):
    sid = _insert_sighting(db_conn)
    match = lambda q: db_conn.execute(
        "SELECT rowid FROM sightings_fts WHERE sightings_fts MATCH ?", (q,)
    ).fetchall()
    assert len(match('"orb"')) == 1
    db_conn.execute("UPDATE sightings SET title='Black triangle craft' WHERE id=?", (sid,))
    db_conn.commit()
    assert len(match('"orb"')) == 0
    assert len(match('"triangle"')) == 1
    db_conn.execute("DELETE FROM sightings WHERE id=?", (sid,))
    db_conn.commit()
    assert len(match('"triangle"')) == 0


def test_media_cascade_delete(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, "uploads/2026/07/aabbccddeeff00112233445566778899.jpg"),
    )
    db_conn.commit()
    db_conn.execute("DELETE FROM sightings WHERE id=?", (sid,))
    db_conn.commit()
    n = db_conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    assert n == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'app.db'`.

- [ ] **Step 3: Implement `app/db.py`**

```python
import sqlite3
from pathlib import Path

from app.config import get_settings

SCHEMA = """
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
  location_text TEXT NOT NULL DEFAULT '',
  city TEXT,
  country TEXT,
  lat REAL,
  lon REAL,
  reddit_post_id TEXT UNIQUE,
  reddit_score INTEGER NOT NULL DEFAULT 0,
  reddit_num_comments INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending_post',
  featured INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_sightings_status_created ON sightings(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sightings_shape ON sightings(shape);
CREATE INDEX IF NOT EXISTS idx_sightings_country ON sightings(country);

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
CREATE INDEX IF NOT EXISTS idx_media_sighting ON media(sighting_id);

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

CREATE VIRTUAL TABLE IF NOT EXISTS sightings_fts USING fts5(
  title, description, location_text,
  content='sightings', content_rowid='id'
);

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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_db():
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py tests/conftest.py
git commit -m "feat: sqlite schema with WAL, cascades, and FTS5 triggers"
```

---

### Task 3: R2 client (presigned uploads, public URLs, object ops)

**Files:**
- Create: `app/r2.py`
- Test: `tests/test_r2.py`

**Interfaces:**
- Produces: `app.r2.make_upload_key(content_type: str) -> str` (format `uploads/YYYY/MM/<32-hex>.<ext>`); `presign_put(key, content_type, size_bytes, expires=900) -> str`; `public_url(key) -> str`; `head_exists(key) -> bool`; `put_bytes(key, data: bytes, content_type) -> None`; `delete_key(key) -> None`; `list_keys(prefix) -> Iterator[tuple[str, datetime]]`; `ALLOWED_IMAGE: dict[str,str]`, `ALLOWED_VIDEO: dict[str,str]` (content-type → extension).
- Note: presigning is pure local computation (no network) — tests need no mocking.

- [ ] **Step 1: Write the failing tests**

`tests/test_r2.py`:
```python
import re
from app import r2


def test_make_upload_key_format():
    key = r2.make_upload_key("image/jpeg")
    assert re.fullmatch(r"uploads/\d{4}/\d{2}/[0-9a-f]{32}\.jpg", key)
    assert r2.make_upload_key("video/mp4").endswith(".mp4")
    assert r2.make_upload_key("video/quicktime").endswith(".mov")


def test_presign_put_is_signed_url_for_key():
    key = "uploads/2026/07/aabbccddeeff00112233445566778899.jpg"
    url = r2.presign_put(key, "image/jpeg", 1000)
    assert url.startswith("https://r2.test/test-bucket/uploads/")
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=900" in url


def test_public_url():
    key = "uploads/2026/07/aabbccddeeff00112233445566778899.jpg"
    assert r2.public_url(key) == f"https://media.test/{key}"


def test_allowed_types():
    assert r2.ALLOWED_IMAGE["image/png"] == ".png"
    assert r2.ALLOWED_VIDEO["video/mp4"] == ".mp4"
    assert "video/mp4" not in r2.ALLOWED_IMAGE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_r2.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'app.r2'`.

- [ ] **Step 3: Implement `app/r2.py`**

```python
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterator

import boto3
from botocore.config import Config

from app.config import get_settings

ALLOWED_IMAGE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_VIDEO = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


@lru_cache
def client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.r2_endpoint,
        aws_access_key_id=s.r2_access_key,
        aws_secret_access_key=s.r2_secret_key,
        config=Config(signature_version="s3v4", region_name="auto"),
    )


def make_upload_key(content_type: str) -> str:
    ext = (ALLOWED_IMAGE | ALLOWED_VIDEO)[content_type]
    now = datetime.now(timezone.utc)
    return f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"


def presign_put(key: str, content_type: str, size_bytes: int, expires: int = 900) -> str:
    s = get_settings()
    return client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": s.r2_bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
        },
        ExpiresIn=expires,
    )


def public_url(key: str) -> str:
    return f"{get_settings().media_base_url}/{key}"


def head_exists(key: str) -> bool:
    try:
        client().head_object(Bucket=get_settings().r2_bucket, Key=key)
        return True
    except client().exceptions.ClientError:
        return False


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    client().put_object(
        Bucket=get_settings().r2_bucket, Key=key, Body=data, ContentType=content_type
    )


def delete_key(key: str) -> None:
    client().delete_object(Bucket=get_settings().r2_bucket, Key=key)


def list_keys(prefix: str) -> Iterator[tuple[str, datetime]]:
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=get_settings().r2_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"], obj["LastModified"]
```

Note: `client()` is lru_cached, so tests that change env must call `r2.client.cache_clear()` — the autouse `_fresh_settings` fixture in conftest should be extended now to also clear it:

```python
@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    try:
        from app import r2
        r2.client.cache_clear()
    except ImportError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_r2.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/r2.py tests/test_r2.py tests/conftest.py
git commit -m "feat: R2 client with presigned PUT uploads"
```

---

### Task 4: Reddit OAuth + sessions/drafts/CSRF

**Files:**
- Create: `app/reddit_oauth.py`, `app/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces (`app/reddit_oauth.py`): `login_url(state: str) -> str`; `exchange_code(code: str) -> str` (access token; raises `AuthError`); `fetch_username(access_token: str) -> str` (raises `AuthError`); `class AuthError(Exception)`.
- Produces (`app/auth.py`): `@dataclass Session(id: str, username: str, access_token: str, expires_at: str)`; `create_session(conn, username, access_token, ttl_seconds) -> str`; `get_session(conn, session_id) -> Session | None` (deletes+returns None when expired); `delete_session(conn, session_id) -> None`; `csrf_for(session_id: str) -> str` (HMAC-SHA256 of session id with `secret_key`, first 32 hex chars); `save_draft(conn, username, form: dict) -> None`; `load_draft(conn, username) -> dict | None`; `delete_draft(conn, username) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_auth.py`:
```python
import httpx
import pytest
import respx

from app import auth, reddit_oauth


def test_login_url_contains_oauth_params():
    url = reddit_oauth.login_url("state-xyz")
    assert url.startswith("https://www.reddit.com/api/v1/authorize?")
    assert "client_id=webapp-id" in url
    assert "state=state-xyz" in url
    assert "duration=temporary" in url
    assert "scope=identity+submit" in url


@respx.mock
def test_exchange_code_returns_token():
    route = respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    assert reddit_oauth.exchange_code("code-1") == "tok-1"
    sent = route.calls[0].request
    assert b"grant_type=authorization_code" in sent.content
    assert sent.headers["User-Agent"].startswith("web:report.ufosighting")


@respx.mock
def test_exchange_code_error_raises():
    respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )
    with pytest.raises(reddit_oauth.AuthError):
        reddit_oauth.exchange_code("bad")


@respx.mock
def test_fetch_username():
    respx.get("https://oauth.reddit.com/api/v1/me").mock(
        return_value=httpx.Response(200, json={"name": "tmosh"})
    )
    assert reddit_oauth.fetch_username("tok-1") == "tmosh"


def test_session_roundtrip_and_expiry(db_conn):
    sid = auth.create_session(db_conn, "tester", "tok-abc", ttl_seconds=3600)
    sess = auth.get_session(db_conn, sid)
    assert sess.username == "tester" and sess.access_token == "tok-abc"

    expired = auth.create_session(db_conn, "old", "tok-old", ttl_seconds=-10)
    assert auth.get_session(db_conn, expired) is None
    # expired session row must be deleted
    n = db_conn.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (expired,)).fetchone()[0]
    assert n == 0


def test_get_session_unknown_id(db_conn):
    assert auth.get_session(db_conn, "nope") is None


def test_csrf_deterministic_and_session_bound():
    a = auth.csrf_for("sid-1")
    assert a == auth.csrf_for("sid-1")
    assert a != auth.csrf_for("sid-2")
    assert len(a) == 32


def test_draft_roundtrip(db_conn):
    auth.save_draft(db_conn, "tester", {"title": "hello", "media_json": "[]"})
    assert auth.load_draft(db_conn, "tester") == {"title": "hello", "media_json": "[]"}
    auth.save_draft(db_conn, "tester", {"title": "updated"})
    assert auth.load_draft(db_conn, "tester") == {"title": "updated"}
    auth.delete_draft(db_conn, "tester")
    assert auth.load_draft(db_conn, "tester") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: ERROR `ModuleNotFoundError` for `app.reddit_oauth` / `app.auth`.

- [ ] **Step 3: Implement `app/reddit_oauth.py`**

```python
import urllib.parse

import httpx

from app.config import get_settings

AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
ME_URL = "https://oauth.reddit.com/api/v1/me"


class AuthError(Exception):
    pass


def login_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.reddit_client_id,
        "response_type": "code",
        "state": state,
        "redirect_uri": s.reddit_redirect_uri,
        "duration": "temporary",
        "scope": "identity submit",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> str:
    s = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": s.reddit_redirect_uri,
        },
        auth=(s.reddit_client_id, s.reddit_client_secret),
        headers={"User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200:
        raise AuthError(f"token exchange failed: HTTP {resp.status_code}")
    token = resp.json().get("access_token")
    if not token:
        raise AuthError(f"token exchange failed: {resp.json()}")
    return token


def fetch_username(access_token: str) -> str:
    s = get_settings()
    resp = httpx.get(
        ME_URL,
        headers={"Authorization": f"bearer {access_token}", "User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200:
        raise AuthError(f"identity fetch failed: HTTP {resp.status_code}")
    return resp.json()["name"]
```

- [ ] **Step 4: Implement `app/auth.py`**

```python
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Session:
    id: str
    username: str
    access_token: str
    expires_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(
    conn: sqlite3.Connection, username: str, access_token: str, ttl_seconds: int
) -> str:
    sid = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(seconds=ttl_seconds)).strftime(ISO)
    conn.execute(
        "INSERT INTO sessions (id, username, access_token, expires_at) VALUES (?,?,?,?)",
        (sid, username, access_token, expires_at),
    )
    conn.commit()
    return sid


def get_session(conn: sqlite3.Connection, session_id: str) -> Session | None:
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now().strftime(ISO):
        delete_session(conn, session_id)
        return None
    return Session(row["id"], row["username"], row["access_token"], row["expires_at"])


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()


def csrf_for(session_id: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, b"csrf:" + session_id.encode(), hashlib.sha256).hexdigest()[:32]


def save_draft(conn: sqlite3.Connection, username: str, form: dict) -> None:
    conn.execute(
        """INSERT INTO drafts (username, form_json, updated_at)
           VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
           ON CONFLICT(username) DO UPDATE SET
             form_json=excluded.form_json, updated_at=excluded.updated_at""",
        (username, json.dumps(form)),
    )
    conn.commit()


def load_draft(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute("SELECT form_json FROM drafts WHERE username=?", (username,)).fetchone()
    return json.loads(row["form_json"]) if row else None


def delete_draft(conn: sqlite3.Connection, username: str) -> None:
    conn.execute("DELETE FROM drafts WHERE username=?", (username,))
    conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add app/reddit_oauth.py app/auth.py tests/test_auth.py
git commit -m "feat: reddit oauth client, server-side sessions, csrf, drafts"
```

---

### Task 5: FastAPI app skeleton, auth routes, base templates, CSS

**Files:**
- Create: `app/web.py`, `app/main.py`, `app/routes/auth.py`, `app/templates/base.html`, `app/templates/login.html`, `static/css/site.css`
- Modify: `tests/conftest.py` (add `client`, `app_db`, `logged_in` fixtures)
- Test: `tests/test_auth_routes.py`

**Interfaces:**
- Consumes: `db.get_db`, `auth.*`, `reddit_oauth.*` from Tasks 2/4.
- Produces: `app.main.create_app(start_thumb_worker: bool = True) -> FastAPI` (uvicorn factory); `app.web.templates` (Jinja2Templates with global `media_url = r2.public_url`); `app.web.current_user(request, conn) -> auth.Session | None` dependency; `app.web.is_admin(user) -> bool`; `app.web.require_admin` dependency (raises 404 for non-admins); routes `GET /auth/login?next=`, `GET /auth/callback`, `GET /auth/logout`; session cookie named `sid`, state cookie `oauth_state` with value `"{state}|{next}"`.
- Produces (conftest): `client` fixture (TestClient, tmp DB via `DB_PATH` env, thumb worker off); `app_db` fixture (connection to the client's DB); `logged_in` fixture (client with a session for user `tester`, exposes `client.sid`).
- Note: every `templates.TemplateResponse` context must include `"user"` (base.html renders it).

- [ ] **Step 1: Write the failing tests**

Add to `tests/conftest.py` (bottom):
```python
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(start_thumb_worker=False)) as c:
        yield c


@pytest.fixture
def app_db(client):
    from app import db
    conn = db.connect(os.environ["DB_PATH"])
    yield conn
    conn.close()


@pytest.fixture
def logged_in(client, app_db):
    from app import auth
    sid = auth.create_session(app_db, "tester", "tok-abc", 3600)
    client.cookies.set("sid", sid)
    client.sid = sid
    return client
```

`tests/test_auth_routes.py`:
```python
import httpx
import respx

from app import auth


def _mock_reddit_login(username="witness1"):
    respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-9"})
    )
    respx.get("https://oauth.reddit.com/api/v1/me").mock(
        return_value=httpx.Response(200, json={"name": username})
    )


def test_login_redirects_to_reddit_with_state_cookie(client):
    r = client.get("/auth/login?next=/submit", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://www.reddit.com/api/v1/authorize?")
    assert "oauth_state" in r.cookies


@respx.mock
def test_callback_creates_session_and_redirects(client):
    _mock_reddit_login()
    client.get("/auth/login?next=/submit", follow_redirects=False)
    state = client.cookies["oauth_state"].split("|")[0]
    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/submit"
    assert "sid" in r.cookies


def test_callback_rejects_state_mismatch(client):
    client.get("/auth/login", follow_redirects=False)
    r = client.get("/auth/callback?code=abc&state=WRONG", follow_redirects=False)
    assert r.status_code == 400


def test_callback_handles_user_denial(client):
    r = client.get("/auth/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 400


@respx.mock
def test_open_redirect_blocked(client):
    _mock_reddit_login()
    client.get("/auth/login?next=//evil.example", follow_redirects=False)
    state = client.cookies["oauth_state"].split("|")[0]
    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_logout_deletes_session(logged_in, app_db):
    sid = logged_in.sid
    assert auth.get_session(app_db, sid) is not None
    r = logged_in.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    assert auth.get_session(app_db, sid) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_auth_routes.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Implement `app/web.py`**

```python
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import auth, db, r2
from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["media_url"] = r2.public_url


def current_user(request: Request, conn=Depends(db.get_db)) -> auth.Session | None:
    sid = request.cookies.get("sid")
    return auth.get_session(conn, sid) if sid else None


def is_admin(user: auth.Session | None) -> bool:
    return bool(user) and user.username.lower() in get_settings().admin_users


def require_admin(user: auth.Session | None = Depends(current_user)) -> auth.Session:
    if not is_admin(user):
        raise HTTPException(status_code=404)
    return user
```

- [ ] **Step 4: Implement `app/main.py`**

```python
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.config import get_settings


def create_app(start_thumb_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(get_settings().db_path)
        db.init_db(conn)
        conn.close()
        stop_event = threading.Event()
        worker = None
        if start_thumb_worker:
            from app import thumbs  # exists from Task 9 on

            worker = thumbs.start_worker(stop_event)
        yield
        stop_event.set()
        if worker:
            worker.join(timeout=15)

    app = FastAPI(title="ufosighting.report", lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent.parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from app.routes import auth as auth_routes

    app.include_router(auth_routes.router)
    return app
```

(Note: running the dev server with the thumb worker enabled requires Task 9; tests always pass `start_thumb_worker=False` until then.)

- [ ] **Step 5: Implement `app/routes/auth.py`**

```python
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app import auth, db, reddit_oauth
from app.config import get_settings
from app.web import templates

router = APIRouter()


def _safe_next(next_url: str) -> str:
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@router.get("/auth/login")
def login(next: str = "/submit"):
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(reddit_oauth.login_url(state), status_code=302)
    resp.set_cookie(
        "oauth_state", f"{state}|{_safe_next(next)}",
        max_age=600, httponly=True, samesite="lax",
    )
    return resp


@router.get("/auth/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    conn=Depends(db.get_db),
):
    def fail(message: str, status: int):
        return templates.TemplateResponse(
            request, "login.html",
            {"user": None, "error": message, "next_url": "/submit"},
            status_code=status,
        )

    if error or not code:
        return fail("Reddit login was cancelled or failed. You can try again.", 400)
    saved = request.cookies.get("oauth_state", "")
    saved_state, _, next_url = saved.partition("|")
    if not state or not saved_state or state != saved_state:
        return fail("Login session mismatch — please try again.", 400)
    try:
        token = reddit_oauth.exchange_code(code)
        username = reddit_oauth.fetch_username(token)
    except reddit_oauth.AuthError:
        return fail("Could not complete Reddit login — please try again.", 502)

    s = get_settings()
    sid = auth.create_session(conn, username, token, s.session_ttl_seconds)
    resp = RedirectResponse(_safe_next(next_url), status_code=303)
    resp.set_cookie(
        "sid", sid, max_age=s.session_ttl_seconds,
        httponly=True, samesite="lax", secure=s.base_url.startswith("https"),
    )
    resp.delete_cookie("oauth_state")
    return resp


@router.get("/auth/logout")
def logout(request: Request, conn=Depends(db.get_db)):
    sid = request.cookies.get("sid")
    if sid:
        auth.delete_session(conn, sid)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("sid")
    return resp
```

- [ ] **Step 6: Create `app/templates/base.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}UFO Sighting Reports — ufosighting.report{% endblock %}</title>
  <link rel="stylesheet" href="/static/css/site.css">
  {% block head %}{% endblock %}
</head>
<body>
<header class="site-header">
  <a class="brand" href="/">&#128065;&#65039; ufosighting<span>.report</span></a>
  <nav>
    <a href="/">Gallery</a>
    <a href="/map">Map</a>
    <a href="/search">Search</a>
    <a class="btn primary" href="/submit">Report a sighting</a>
    {% if user %}
      <span class="whoami">u/{{ user.username }}</span>
      <a href="/auth/logout">Log out</a>
    {% endif %}
  </nav>
</header>
<main>
{% block content %}{% endblock %}
</main>
<footer class="site-footer">
  <p>Sighting reports from the <a href="https://www.reddit.com/r/UFOs/">r/UFOs</a> community.</p>
</footer>
</body>
</html>
```

- [ ] **Step 7: Create `app/templates/login.html`**

```html
{% extends "base.html" %}
{% block title %}Log in with Reddit — ufosighting.report{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>Log in with Reddit</h1>
  {% if error %}<p class="flash error">{{ error }}</p>{% endif %}
  <p>To report a sighting you log in with your Reddit account. Your report is
     posted to the subreddit <strong>as you</strong>, so replies land in your
     Reddit inbox.</p>
  <p class="muted">We see only your username and a one-hour posting permission.
     No password, nothing stored long-term.</p>
  <a class="btn primary" href="/auth/login?next={{ next_url or '/submit' }}">Continue with Reddit</a>
</section>
{% endblock %}
```

- [ ] **Step 8: Create `static/css/site.css`**

```css
:root {
  --bg: #0b0e14; --bg2: #121724; --panel: #171d2e; --line: #232b42;
  --text: #dbe2f4; --muted: #8b96b5; --accent: #6ee7a0; --accent2: #7aa2ff;
  --danger: #ff7a7a; --radius: 10px;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
a { color: var(--accent2); text-decoration: none; }
a:hover { text-decoration: underline; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }

.site-header {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 12px 20px; background: var(--bg2); border-bottom: 1px solid var(--line);
  flex-wrap: wrap;
}
.brand { font-weight: 700; font-size: 1.1rem; color: var(--text); }
.brand span { color: var(--muted); font-weight: 400; }
.site-header nav { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.site-header nav a { color: var(--text); }
.whoami { color: var(--muted); }
.site-footer { text-align: center; color: var(--muted); padding: 24px; border-top: 1px solid var(--line); }

.btn {
  display: inline-block; padding: 8px 16px; border-radius: var(--radius);
  border: 1px solid var(--line); background: var(--panel); color: var(--text);
  cursor: pointer; font-size: 0.95rem;
}
.btn.primary { background: var(--accent); border-color: var(--accent); color: #05240f; font-weight: 600; }
.btn.primary:hover { filter: brightness(1.1); text-decoration: none; }
.btn.danger { border-color: var(--danger); color: var(--danger); }

.panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 24px; }
.panel.narrow { max-width: 560px; margin: 48px auto; }
.muted { color: var(--muted); }
.flash.error { background: #3a1520; border: 1px solid var(--danger); color: #ffc9c9; padding: 10px 14px; border-radius: var(--radius); }

.filters { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
.filters select, .filters input, form.form input, form.form select, form.form textarea {
  background: var(--bg2); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px; font: inherit;
}

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }
.card {
  position: relative; display: block; background: var(--panel); color: var(--text);
  border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden;
}
.card:hover { border-color: var(--accent2); text-decoration: none; }
.card img, .card .noimg { width: 100%; aspect-ratio: 4/3; object-fit: cover; display: block; }
.card .noimg { display: flex; align-items: center; justify-content: center; font-size: 2.4rem; background: var(--bg2); }
.card .badge-video {
  position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,.65);
  border-radius: 6px; padding: 2px 8px; font-size: .85rem;
}
.card .meta { padding: 10px 12px 12px; }
.card h3 { margin: 0 0 4px; font-size: 1rem; line-height: 1.3; }
.card p { margin: 2px 0; font-size: .85rem; color: var(--muted); }

.pager { display: flex; gap: 12px; justify-content: center; margin-top: 24px; }

.detail { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; }
@media (max-width: 900px) { .detail { grid-template-columns: 1fr; } }
.viewer img, .viewer video { width: 100%; border-radius: var(--radius); background: #000; }
.thumbstrip { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.thumbstrip img { width: 90px; height: 68px; object-fit: cover; border-radius: 6px; cursor: pointer; border: 2px solid transparent; }
.thumbstrip img.active { border-color: var(--accent); }
.facts { border-collapse: collapse; width: 100%; }
.facts td { padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.facts td:first-child { color: var(--muted); white-space: nowrap; }

form.form { display: grid; gap: 14px; max-width: 720px; }
form.form label { display: grid; gap: 6px; font-size: .95rem; }
form.form .row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
form.form textarea { min-height: 140px; resize: vertical; }
.dropzone {
  border: 2px dashed var(--line); border-radius: var(--radius); padding: 28px;
  text-align: center; color: var(--muted); cursor: pointer;
}
.dropzone.drag { border-color: var(--accent); color: var(--accent); }
.filelist { display: grid; gap: 8px; }
.filelist .file {
  display: flex; align-items: center; gap: 10px; background: var(--bg2);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font-size: .9rem;
}
.filelist progress { flex: 1; height: 8px; }
.filelist .err { color: var(--danger); }

#map { height: 70vh; border-radius: var(--radius); border: 1px solid var(--line); }
.empty { color: var(--muted); padding: 40px 0; text-align: center; grid-column: 1/-1; }
table.admin { width: 100%; border-collapse: collapse; }
table.admin td, table.admin th { padding: 8px; border-bottom: 1px solid var(--line); text-align: left; font-size: .9rem; }
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auth_routes.py -v`
Expected: 6 passed. Also run the full suite: `.venv/bin/pytest -q` — all green.

- [ ] **Step 10: Commit**

```bash
git add app/web.py app/main.py app/routes/auth.py app/templates/ static/ tests/
git commit -m "feat: app factory, reddit login/logout routes, base templates"
```

---

### Task 6: Reddit API module (submit-as-user, script token, post info)

**Files:**
- Create: `app/reddit.py`
- Test: `tests/test_reddit.py`

**Interfaces:**
- Consumes: `get_settings()` (subreddit, script creds, user_agent).
- Produces: `submit_post(access_token: str, *, subreddit: str, title: str, body: str, flair_id: str = "") -> str` (returns bare post id, e.g. `"1abcde"`); exceptions `RedditError`, `TokenExpired(RedditError)` (401 only), `RateLimited(RedditError)`; `script_token() -> str` (module-cached with expiry, `_script_token` dict resettable in tests); `PostInfo` dataclass `(removed_by_category: str | None, score: int, num_comments: int)`; `fetch_posts_info(post_ids: list[str]) -> dict[str, PostInfo]` (batches of 100 via `/api/info`); `status_from_removed_by_category(rbc: str | None) -> str` (`None→'live'`, `'deleted'→'deleted_by_user'`, anything else→`'removed_on_reddit'`).

- [ ] **Step 1: Write the failing tests**

`tests/test_reddit.py`:
```python
import httpx
import pytest
import respx

from app import reddit


@pytest.fixture(autouse=True)
def _reset_script_token():
    reddit._script_token.update(token=None, expires=0.0)
    yield


def _submit_ok(post_fullname="t3_1abcde"):
    return httpx.Response(
        200,
        json={"json": {"errors": [], "data": {"name": post_fullname, "url": "https://reddit.com/x"}}},
    )


@respx.mock
def test_submit_post_success_returns_bare_id():
    route = respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    post_id = reddit.submit_post(
        "tok-1", subreddit="UFOs_sandbox", title="Orb over lake", body="body text", flair_id="flair-123"
    )
    assert post_id == "1abcde"
    sent = route.calls[0].request
    assert b"sr=UFOs_sandbox" in sent.content
    assert b"kind=self" in sent.content
    assert b"flair_id=flair-123" in sent.content
    assert sent.headers["Authorization"] == "bearer tok-1"


@respx.mock
def test_submit_post_omits_empty_flair():
    route = respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T"*10, body="b")
    assert b"flair_id" not in route.calls[0].request.content


@respx.mock
def test_submit_post_ratelimit_raises():
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "you are doing that too much. try again in 9 minutes.", "ratelimit"]
        ]}})
    )
    with pytest.raises(reddit.RateLimited, match="9 minutes"):
        reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T"*10, body="b")


@respx.mock
def test_submit_post_401_raises_token_expired():
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=httpx.Response(401))
    with pytest.raises(reddit.TokenExpired):
        reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T"*10, body="b")


@respx.mock
def test_submit_post_403_is_plain_error_not_token_expired():
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=httpx.Response(403))
    with pytest.raises(reddit.RedditError) as exc_info:
        reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T"*10, body="b")
    assert not isinstance(exc_info.value, reddit.TokenExpired)


@respx.mock
def test_script_token_cached_across_calls():
    route = respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "stok", "expires_in": 3600})
    )
    assert reddit.script_token() == "stok"
    assert reddit.script_token() == "stok"
    assert route.call_count == 1


@respx.mock
def test_fetch_posts_info_parses_children():
    respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "stok", "expires_in": 3600})
    )
    respx.get("https://oauth.reddit.com/api/info").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "aaa", "removed_by_category": None, "score": 42, "num_comments": 7}},
            {"data": {"id": "bbb", "removed_by_category": "moderator", "score": 1, "num_comments": 0}},
        ]}})
    )
    infos = reddit.fetch_posts_info(["aaa", "bbb"])
    assert infos["aaa"].score == 42 and infos["aaa"].removed_by_category is None
    assert infos["bbb"].removed_by_category == "moderator"


def test_fetch_posts_info_empty_list_no_network():
    assert reddit.fetch_posts_info([]) == {}


def test_status_mapping():
    f = reddit.status_from_removed_by_category
    assert f(None) == "live"
    assert f("deleted") == "deleted_by_user"
    for rbc in ("moderator", "automod_filtered", "reddit", "spam", "content_takedown"):
        assert f(rbc) == "removed_on_reddit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_reddit.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'app.reddit'`.

- [ ] **Step 3: Implement `app/reddit.py`**

```python
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SUBMIT_URL = "https://oauth.reddit.com/api/submit"
INFO_URL = "https://oauth.reddit.com/api/info"


class RedditError(Exception):
    pass


class TokenExpired(RedditError):
    pass


class RateLimited(RedditError):
    pass


def _headers(token: str) -> dict:
    return {"Authorization": f"bearer {token}", "User-Agent": get_settings().user_agent}


def submit_post(
    access_token: str, *, subreddit: str, title: str, body: str, flair_id: str = ""
) -> str:
    data = {
        "sr": subreddit,
        "kind": "self",
        "title": title[:300],
        "text": body,
        "api_type": "json",
        "sendreplies": "true",
        "resubmit": "true",
    }
    if flair_id:
        data["flair_id"] = flair_id
    resp = httpx.post(SUBMIT_URL, data=data, headers=_headers(access_token), timeout=30)
    if resp.status_code == 401:
        raise TokenExpired("Reddit session expired")
    if resp.status_code != 200:
        raise RedditError(f"reddit submit failed: HTTP {resp.status_code}")
    j = resp.json().get("json", {})
    errors = j.get("errors") or []
    if errors:
        code = errors[0][0]
        msg = errors[0][1] if len(errors[0]) > 1 else code
        if code == "RATELIMIT":
            raise RateLimited(msg)
        raise RedditError(f"{code}: {msg}")
    name = (j.get("data") or {}).get("name", "")
    if not name.startswith("t3_"):
        raise RedditError(f"unexpected submit response: {j}")
    return name.removeprefix("t3_")


_script_token: dict = {"token": None, "expires": 0.0}


def script_token() -> str:
    if _script_token["token"] and time.time() < _script_token["expires"] - 60:
        return _script_token["token"]
    s = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={"grant_type": "password", "username": s.script_username, "password": s.script_password},
        auth=(s.script_client_id, s.script_client_secret),
        headers={"User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200 or "access_token" not in resp.json():
        raise RedditError(f"script token failed: HTTP {resp.status_code}")
    data = resp.json()
    _script_token["token"] = data["access_token"]
    _script_token["expires"] = time.time() + float(data.get("expires_in", 3600))
    return _script_token["token"]


@dataclass
class PostInfo:
    removed_by_category: str | None
    score: int
    num_comments: int


def fetch_posts_info(post_ids: list[str]) -> dict[str, PostInfo]:
    out: dict[str, PostInfo] = {}
    if not post_ids:
        return out
    token = script_token()
    for i in range(0, len(post_ids), 100):
        chunk = post_ids[i : i + 100]
        resp = httpx.get(
            INFO_URL,
            params={"id": ",".join("t3_" + pid for pid in chunk)},
            headers=_headers(token),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RedditError(f"info fetch failed: HTTP {resp.status_code}")
        for child in resp.json()["data"]["children"]:
            d = child["data"]
            out[d["id"]] = PostInfo(
                removed_by_category=d.get("removed_by_category"),
                score=int(d.get("score", 0)),
                num_comments=int(d.get("num_comments", 0)),
            )
    return out


def status_from_removed_by_category(rbc: str | None) -> str:
    if rbc is None:
        return "live"
    if rbc == "deleted":
        return "deleted_by_user"
    return "removed_on_reddit"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_reddit.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add app/reddit.py tests/test_reddit.py
git commit -m "feat: reddit submit-as-user, script token, post info fetch"
```

---

### Task 7: Presign endpoint

**Files:**
- Create: `app/routes/submit.py` (presign endpoint only; Task 8 extends this file)
- Modify: `app/main.py` (include submit router)
- Test: `tests/test_presign.py`

**Interfaces:**
- Consumes: `web.current_user`, `r2.ALLOWED_IMAGE/ALLOWED_VIDEO/make_upload_key/presign_put/public_url`, settings caps.
- Produces: `POST /api/presign` accepting JSON `{"filename": str, "content_type": str, "size_bytes": int}`, returning `{"key", "upload_url", "public_url", "kind"}`; 401 when anonymous, 400 for bad type/size. `static/js/upload.js` (Task 8) is the consumer.

- [ ] **Step 1: Write the failing tests**

`tests/test_presign.py`:
```python
GOOD = {"filename": "orb.jpg", "content_type": "image/jpeg", "size_bytes": 5_000_000}


def test_presign_requires_login(client):
    assert client.post("/api/presign", json=GOOD).status_code == 401


def test_presign_success_image(logged_in):
    r = logged_in.post("/api/presign", json=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "image"
    assert body["key"].startswith("uploads/") and body["key"].endswith(".jpg")
    assert "X-Amz-Signature=" in body["upload_url"]
    assert body["public_url"] == f"https://media.test/{body['key']}"


def test_presign_success_video(logged_in):
    r = logged_in.post(
        "/api/presign",
        json={"filename": "ufo.mp4", "content_type": "video/mp4", "size_bytes": 100_000_000},
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "video"


def test_presign_rejects_unsupported_type(logged_in):
    r = logged_in.post(
        "/api/presign",
        json={"filename": "x.exe", "content_type": "application/octet-stream", "size_bytes": 100},
    )
    assert r.status_code == 400


def test_presign_rejects_oversize(logged_in):
    r = logged_in.post(
        "/api/presign",
        json={"filename": "big.jpg", "content_type": "image/jpeg", "size_bytes": 26 * 1024 * 1024},
    )
    assert r.status_code == 400
    r = logged_in.post(
        "/api/presign",
        json={"filename": "big.mp4", "content_type": "video/mp4", "size_bytes": 501 * 1024 * 1024},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_presign.py -v`
Expected: FAIL — 404s (route not registered).

- [ ] **Step 3: Implement `app/routes/submit.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import r2
from app.config import get_settings
from app.web import current_user

router = APIRouter()


class PresignRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


@router.post("/api/presign")
def presign(req: PresignRequest, user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Log in with Reddit first")
    s = get_settings()
    if req.content_type in r2.ALLOWED_IMAGE:
        kind, cap = "image", s.max_image_bytes
    elif req.content_type in r2.ALLOWED_VIDEO:
        kind, cap = "video", s.max_video_bytes
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {req.content_type}")
    if not 0 < req.size_bytes <= cap:
        raise HTTPException(
            status_code=400,
            detail=f"File too large — max {cap // (1024 * 1024)}MB for {kind}s",
        )
    key = r2.make_upload_key(req.content_type)
    return {
        "key": key,
        "upload_url": r2.presign_put(key, req.content_type, req.size_bytes),
        "public_url": r2.public_url(key),
        "kind": kind,
    }
```

Modify `app/main.py` — after the auth router include, add:
```python
    from app.routes import submit as submit_routes

    app.include_router(submit_routes.router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_presign.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/routes/submit.py app/main.py tests/test_presign.py
git commit -m "feat: presigned R2 upload endpoint"
```

---

### Task 8: Helpers + full submission flow (form, validation, post-as-user, drafts)

**Files:**
- Create: `app/helpers.py`, `app/templates/submit.html`, `static/js/upload.js`
- Modify: `app/routes/submit.py` (add GET/POST `/submit`), `app/web.py` (register template filters)
- Test: `tests/test_helpers.py`, `tests/test_submit.py`

**Interfaces:**
- Consumes: `reddit.submit_post/TokenExpired/RateLimited/RedditError`, `auth.csrf_for/save_draft/load_draft/delete_draft`, `r2.head_exists/public_url`, `db.get_db`, `web.current_user/templates`.
- Produces (`app/helpers.py`): `SHAPES: list[str]` = `["light","orb","sphere","disc","triangle","cigar","tic-tac","chevron","formation","other"]`; `slugify(text, max_len=60) -> str` (fallback `"sighting"`); `humanize_duration(seconds: int | None) -> str`; `to_utc(date_str, time_str, tz_name) -> datetime`; `from_utc(utc_str, tz_name) -> str` (`"YYYY-MM-DD HH:MM"` local); `format_post_body(*, description, sighted_local, tz_name, location_line, shape, duration_seconds, witnesses, media_urls, gallery_url) -> str`; `ISO = "%Y-%m-%dT%H:%M:%SZ"`.
- Produces (`app/routes/submit.py`): `GET /submit` (login page when anonymous; form prefilled from draft when present); `POST /submit` (form-encoded; the flow below); `validate_submission(form: dict) -> tuple[dict, list[str]]`; `KEY_RE` regex `^uploads/\d{4}/\d{2}/[0-9a-f]{32}\.[a-z0-9]{2,5}$`.
- Form field names (used by template, upload.js, tests): `csrf_token, title, description, sighted_date, sighted_time, tz_name, location_text, city, country, shape, duration_value, duration_unit (seconds|minutes|hours), witnesses, lat, lon, media_json`.
- `validate_submission` returns `clean` dict with keys: `title, description, sighted_at (UTC ISO str), tz_name, duration_seconds (int|None), shape (str|None), witnesses (int|None), location_text, city (str|None), country (str|None), lat (float|None), lon (float|None), media (list[dict(key, kind, width, height, size_bytes)])`.

**POST /submit flow (the heart of Phase 1):**
1. anonymous → 303 to `/auth/login?next=/submit`
2. CSRF mismatch → 403
3. validation errors or missing R2 object → re-render form 422 with errors + entered values
4. INSERT sighting (`status='pending_post'`) + media rows, commit — id now known for the gallery URL
5. `reddit.submit_post(...)` as the user
   - `TokenExpired` → save draft, delete pending row, 303 to login
   - `RateLimited` → delete pending row, re-render 429 with Reddit's message
   - `RedditError` → delete pending row, re-render 502
6. success → UPDATE `reddit_post_id` + `status='live'`, delete draft, 303 to `/sighting/{id}/{slug}`

- [ ] **Step 1: Write the failing helper tests**

`tests/test_helpers.py`:
```python
from app import helpers


def test_slugify():
    assert helpers.slugify("Bright ORB over the lake!!") == "bright-orb-over-the-lake"
    assert helpers.slugify("???") == "sighting"
    assert len(helpers.slugify("x" * 500)) <= 60


def test_humanize_duration():
    assert helpers.humanize_duration(None) == ""
    assert helpers.humanize_duration(1) == "1 second"
    assert helpers.humanize_duration(45) == "45 seconds"
    assert helpers.humanize_duration(120) == "2 minutes"
    assert helpers.humanize_duration(5400) == "1.5 hours"
    assert helpers.humanize_duration(7200) == "2 hours"


def test_to_utc_and_back():
    dt = helpers.to_utc("2026-07-01", "22:15", "America/Vancouver")
    assert dt.strftime(helpers.ISO) == "2026-07-02T05:15:00Z"
    assert helpers.from_utc("2026-07-02T05:15:00Z", "America/Vancouver") == "2026-07-01 22:15"


def test_format_post_body():
    body = helpers.format_post_body(
        description="A silent orange orb hovered.",
        sighted_local="2026-07-01 22:15", tz_name="America/Vancouver",
        location_line="Lake Cowichan, BC, Canada", shape="orb",
        duration_seconds=120, witnesses=2,
        media_urls=["https://media.test/uploads/2026/07/aa.jpg"],
        gallery_url="https://ufosighting.report/sighting/1/bright-orb",
    )
    assert "**When:** 2026-07-01 22:15 (America/Vancouver)" in body
    assert "**Where:** Lake Cowichan, BC, Canada" in body
    assert "**Duration:** 2 minutes" in body
    assert "- https://media.test/uploads/2026/07/aa.jpg" in body
    assert "https://ufosighting.report/sighting/1/bright-orb" in body


def test_format_post_body_skips_empty_fields():
    body = helpers.format_post_body(
        description="d", sighted_local="2026-07-01 22:15", tz_name="UTC",
        location_line="", shape=None, duration_seconds=None, witnesses=None,
        media_urls=[], gallery_url="https://x/1",
    )
    assert "**Where:**" not in body
    assert "**Shape:**" not in body
    assert "**Media:**" not in body
```

- [ ] **Step 2: Run helper tests to verify they fail**

Run: `.venv/bin/pytest tests/test_helpers.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'app.helpers'`.

- [ ] **Step 3: Implement `app/helpers.py`**

```python
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ISO = "%Y-%m-%dT%H:%M:%SZ"

SHAPES = ["light", "orb", "sphere", "disc", "triangle", "cigar", "tic-tac", "chevron", "formation", "other"]


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "sighting"


def humanize_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600
    hours_str = f"{hours:.1f}".rstrip("0").rstrip(".")
    return f"{hours_str} hour{'s' if hours != 1 else ''}"


def to_utc(date_str: str, time_str: str, tz_name: str) -> datetime:
    local = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def from_utc(utc_str: str, tz_name: str) -> str:
    dt = datetime.strptime(utc_str, ISO).replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")


def format_post_body(
    *, description, sighted_local, tz_name, location_line, shape,
    duration_seconds, witnesses, media_urls, gallery_url,
) -> str:
    facts = [f"**When:** {sighted_local} ({tz_name})"]
    if location_line:
        facts.append(f"**Where:** {location_line}")
    if shape:
        facts.append(f"**Shape:** {shape}")
    if duration_seconds:
        facts.append(f"**Duration:** {humanize_duration(duration_seconds)}")
    if witnesses:
        facts.append(f"**Witnesses:** {witnesses}")
    parts = ["  \n".join(facts), description.strip()]
    if media_urls:
        parts.append("**Media:**\n\n" + "\n".join(f"- {u}" for u in media_urls))
    parts.append(
        f"[View this sighting in the gallery]({gallery_url}) — "
        f"*submitted via [ufosighting.report](https://ufosighting.report)*"
    )
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run helper tests to verify they pass**

Run: `.venv/bin/pytest tests/test_helpers.py -v`
Expected: 5 passed.

- [ ] **Step 5: Write the failing submission-flow tests**

`tests/test_submit.py`:
```python
import json

import httpx
import pytest
import respx

from app import auth

MEDIA_KEY = "uploads/2026/07/" + "a" * 32 + ".jpg"


def good_form(sid: str) -> dict:
    return {
        "csrf_token": auth.csrf_for(sid),
        "title": "Bright orb over the lake",
        "description": "A silent orange orb hovered for two minutes then shot straight up.",
        "sighted_date": "2026-07-01",
        "sighted_time": "22:15",
        "tz_name": "America/Vancouver",
        "location_text": "Lake Cowichan, BC",
        "city": "Lake Cowichan",
        "country": "Canada",
        "shape": "orb",
        "duration_value": "2",
        "duration_unit": "minutes",
        "witnesses": "2",
        "lat": "48.825",
        "lon": "-124.05",
        "media_json": json.dumps(
            [{"key": MEDIA_KEY, "kind": "image", "width": 1920, "height": 1080, "size_bytes": 123456}]
        ),
    }


@pytest.fixture(autouse=True)
def _media_exists(monkeypatch):
    monkeypatch.setattr("app.routes.submit.r2.head_exists", lambda key: True)


def _submit_ok():
    return httpx.Response(
        200, json={"json": {"errors": [], "data": {"name": "t3_1abcde", "url": "https://reddit/x"}}}
    )


def test_get_submit_anonymous_shows_login(client):
    r = client.get("/submit")
    assert r.status_code == 200
    assert "Continue with Reddit" in r.text


def test_get_submit_logged_in_shows_form(logged_in):
    r = logged_in.get("/submit")
    assert r.status_code == 200
    assert 'name="csrf_token"' in r.text
    assert 'name="media_json"' in r.text


@respx.mock
def test_happy_path_posts_to_reddit_and_goes_live(logged_in, app_db):
    route = respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    r = logged_in.post("/submit", data=good_form(logged_in.sid), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sighting/1/bright-orb-over-the-lake"

    row = app_db.execute("SELECT * FROM sightings WHERE id=1").fetchone()
    assert row["status"] == "live"
    assert row["reddit_post_id"] == "1abcde"
    assert row["reddit_username"] == "tester"
    assert row["sighted_at"] == "2026-07-02T05:15:00Z"
    assert row["duration_seconds"] == 120
    media = app_db.execute("SELECT * FROM media WHERE sighting_id=1").fetchall()
    assert len(media) == 1 and media[0]["r2_key"] == MEDIA_KEY

    sent = route.calls[0].request
    assert b"sr=UFOs_sandbox" in sent.content
    assert sent.headers["Authorization"] == "bearer tok-abc"


def test_bad_csrf_rejected(logged_in):
    form = good_form(logged_in.sid)
    form["csrf_token"] = "forged"
    assert logged_in.post("/submit", data=form).status_code == 403


def test_validation_errors_rerender_form(logged_in, app_db):
    form = good_form(logged_in.sid)
    form["title"] = "hi"  # too short
    r = logged_in.post("/submit", data=form)
    assert r.status_code == 422
    assert "Title must be" in r.text
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_bad_media_key_rejected(logged_in):
    form = good_form(logged_in.sid)
    form["media_json"] = json.dumps([{"key": "../../etc/passwd", "kind": "image"}])
    assert logged_in.post("/submit", data=form).status_code == 422


@respx.mock
def test_ratelimit_rolls_back_and_shows_message(logged_in, app_db):
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "try again in 9 minutes", "ratelimit"]]}})
    )
    r = logged_in.post("/submit", data=good_form(logged_in.sid))
    assert r.status_code == 429
    assert "9 minutes" in r.text
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


@respx.mock
def test_token_expired_saves_draft_and_redirects_to_login(logged_in, app_db):
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=httpx.Response(401))
    r = logged_in.post("/submit", data=good_form(logged_in.sid), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login?next=/submit"
    draft = auth.load_draft(app_db, "tester")
    assert draft["title"] == "Bright orb over the lake"
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0
```

- [ ] **Step 6: Run submission tests to verify they fail**

Run: `.venv/bin/pytest tests/test_submit.py -v`
Expected: FAIL — `GET /submit` 404 (route doesn't exist yet).

- [ ] **Step 7: Extend `app/routes/submit.py` with the submission flow**

Replace the imports block and append the routes (keep the presign endpoint from Task 7):

```python
import hmac
import json
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import auth, db, helpers, r2, reddit
from app.config import get_settings
from app.web import current_user, templates

router = APIRouter()

KEY_RE = re.compile(r"^uploads/\d{4}/\d{2}/[0-9a-f]{32}\.[a-z0-9]{2,5}$")

# ... PresignRequest + presign endpoint from Task 7 unchanged ...


def validate_submission(form: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    clean: dict = {}

    clean["title"] = (form.get("title") or "").strip()
    if not 5 <= len(clean["title"]) <= 300:
        errors.append("Title must be 5-300 characters.")

    clean["description"] = (form.get("description") or "").strip()
    if len(clean["description"]) < 10:
        errors.append("Description must be at least 10 characters.")

    tz_name = (form.get("tz_name") or "UTC").strip()
    try:
        ZoneInfo(tz_name)
    except Exception:
        errors.append("Unknown timezone.")
        tz_name = "UTC"
    clean["tz_name"] = tz_name

    try:
        clean["sighted_at"] = helpers.to_utc(
            form.get("sighted_date", ""), form.get("sighted_time", ""), tz_name
        ).strftime(helpers.ISO)
    except (ValueError, TypeError):
        errors.append("Enter a valid date and time.")
        clean["sighted_at"] = None

    shape = (form.get("shape") or "").strip() or None
    if shape and shape not in helpers.SHAPES:
        errors.append("Unknown shape.")
    clean["shape"] = shape

    clean["duration_seconds"] = None
    if (form.get("duration_value") or "").strip():
        try:
            value = float(form["duration_value"])
            unit = form.get("duration_unit", "seconds")
            factor = {"seconds": 1, "minutes": 60, "hours": 3600}[unit]
            seconds = int(value * factor)
            if not 1 <= seconds <= 86400:
                raise ValueError
            clean["duration_seconds"] = seconds
        except (ValueError, KeyError):
            errors.append("Enter a valid duration.")

    clean["witnesses"] = None
    if (form.get("witnesses") or "").strip():
        try:
            witnesses = int(form["witnesses"])
            if not 1 <= witnesses <= 1000:
                raise ValueError
            clean["witnesses"] = witnesses
        except ValueError:
            errors.append("Enter a valid witness count.")

    clean["location_text"] = (form.get("location_text") or "").strip()
    if len(clean["location_text"]) < 2:
        errors.append("Enter a location.")
    clean["city"] = (form.get("city") or "").strip() or None
    clean["country"] = (form.get("country") or "").strip() or None

    clean["lat"], clean["lon"] = None, None
    lat_raw, lon_raw = (form.get("lat") or "").strip(), (form.get("lon") or "").strip()
    if lat_raw or lon_raw:
        try:
            lat, lon = float(lat_raw), float(lon_raw)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
            clean["lat"], clean["lon"] = lat, lon
        except ValueError:
            errors.append("Map pin coordinates are invalid.")

    clean["media"] = []
    raw = form.get("media_json") or "[]"
    try:
        items = json.loads(raw)
        assert isinstance(items, list)
    except (ValueError, AssertionError):
        errors.append("Media list is corrupted — please re-upload.")
        items = []
    if len(items) > get_settings().max_files:
        errors.append(f"At most {get_settings().max_files} files per sighting.")
        items = []
    for item in items:
        key, kind = str(item.get("key", "")), str(item.get("kind", ""))
        if not KEY_RE.fullmatch(key) or kind not in ("image", "video"):
            errors.append("An uploaded file reference is invalid — please re-upload.")
            break
        clean["media"].append(
            {
                "key": key,
                "kind": kind,
                "width": item.get("width"),
                "height": item.get("height"),
                "size_bytes": item.get("size_bytes"),
            }
        )
    return clean, errors


def _render_form(request, user, values, errors, status_code=200):
    return templates.TemplateResponse(
        request,
        "submit.html",
        {
            "user": user,
            "values": values,
            "errors": errors,
            "shapes": helpers.SHAPES,
            "csrf_token": auth.csrf_for(user.id),
            "max_files": get_settings().max_files,
        },
        status_code=status_code,
    )


@router.get("/submit")
def submit_form(request: Request, conn=Depends(db.get_db), user=Depends(current_user)):
    if user is None:
        return templates.TemplateResponse(
            request, "login.html", {"user": None, "next_url": "/submit"}
        )
    values = auth.load_draft(conn, user.username) or {}
    return _render_form(request, user, values, errors=[])


@router.post("/submit")
async def submit_create(request: Request, conn=Depends(db.get_db), user=Depends(current_user)):
    if user is None:
        return RedirectResponse("/auth/login?next=/submit", status_code=303)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    if not hmac.compare_digest(form.get("csrf_token", ""), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")

    clean, errors = validate_submission(form)
    for m in clean["media"]:
        if not r2.head_exists(m["key"]):
            errors.append("An uploaded file was not found in storage — please re-upload.")
            break
    if errors:
        return _render_form(request, user, form, errors, status_code=422)

    s = get_settings()
    cur = conn.execute(
        """INSERT INTO sightings
             (reddit_username, title, description, sighted_at, tz_name, duration_seconds,
              shape, witnesses, location_text, city, country, lat, lon, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_post')""",
        (
            user.username, clean["title"], clean["description"], clean["sighted_at"],
            clean["tz_name"], clean["duration_seconds"], clean["shape"], clean["witnesses"],
            clean["location_text"], clean["city"], clean["country"], clean["lat"], clean["lon"],
        ),
    )
    sighting_id = cur.lastrowid
    for i, m in enumerate(clean["media"]):
        conn.execute(
            """INSERT INTO media (sighting_id, r2_key, kind, width, height, size_bytes, sort_order)
               VALUES (?,?,?,?,?,?,?)""",
            (sighting_id, m["key"], m["kind"], m["width"], m["height"], m["size_bytes"], i),
        )
    conn.commit()

    def rollback():
        conn.execute("DELETE FROM sightings WHERE id=?", (sighting_id,))
        conn.commit()

    slug = helpers.slugify(clean["title"])
    gallery_url = f"{s.base_url}/sighting/{sighting_id}/{slug}"
    location_line = ", ".join(
        part for part in (clean["location_text"], clean["city"], clean["country"]) if part
    )
    body = helpers.format_post_body(
        description=clean["description"],
        sighted_local=helpers.from_utc(clean["sighted_at"], clean["tz_name"]),
        tz_name=clean["tz_name"],
        location_line=location_line,
        shape=clean["shape"],
        duration_seconds=clean["duration_seconds"],
        witnesses=clean["witnesses"],
        media_urls=[r2.public_url(m["key"]) for m in clean["media"]],
        gallery_url=gallery_url,
    )
    try:
        post_id = reddit.submit_post(
            user.access_token,
            subreddit=s.subreddit,
            title=clean["title"],
            body=body,
            flair_id=s.sighting_flair_id,
        )
    except reddit.TokenExpired:
        auth.save_draft(conn, user.username, form)
        rollback()
        return RedirectResponse("/auth/login?next=/submit", status_code=303)
    except reddit.RateLimited as exc:
        rollback()
        return _render_form(request, user, form, [f"Reddit rate limit: {exc}"], status_code=429)
    except reddit.RedditError as exc:
        rollback()
        return _render_form(
            request, user, form, [f"Posting to Reddit failed: {exc}"], status_code=502
        )

    conn.execute(
        "UPDATE sightings SET reddit_post_id=?, status='live' WHERE id=?", (post_id, sighting_id)
    )
    conn.commit()
    auth.delete_draft(conn, user.username)
    return RedirectResponse(f"/sighting/{sighting_id}/{slug}", status_code=303)
```

Also modify `app/web.py` — after the `media_url` global, register helper filters (used by templates in Tasks 10-11):
```python
from app import helpers

templates.env.filters["duration_h"] = helpers.humanize_duration
templates.env.globals["slugify"] = helpers.slugify
```

- [ ] **Step 8: Create `app/templates/submit.html`**

```html
{% extends "base.html" %}
{% block title %}Report a sighting — ufosighting.report{% endblock %}
{% block content %}
<section class="panel">
  <h1>Report a UFO sighting</h1>
  <p class="muted">Posted to r/UFOs as <strong>u/{{ user.username }}</strong> — replies land in your Reddit inbox.</p>
  {% for e in errors %}<p class="flash error">{{ e }}</p>{% endfor %}
  <form class="form" method="post" action="/submit" id="sighting-form">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <input type="hidden" name="media_json" id="media_json" value="{{ values.media_json or '[]' }}">
    <input type="hidden" name="tz_name" id="tz_name" value="{{ values.tz_name or '' }}">
    <input type="hidden" name="lat" id="lat" value="{{ values.lat or '' }}">
    <input type="hidden" name="lon" id="lon" value="{{ values.lon or '' }}">

    <label>Title
      <input name="title" required minlength="5" maxlength="300" value="{{ values.title or '' }}"
             placeholder="e.g. Three orange orbs in triangle formation over Victoria, BC">
    </label>
    <label>What happened?
      <textarea name="description" required minlength="10"
                placeholder="What did you see? Movement, sound, weather, how it disappeared…">{{ values.description or '' }}</textarea>
    </label>
    <div class="row">
      <label>Date <input type="date" name="sighted_date" required value="{{ values.sighted_date or '' }}"></label>
      <label>Local time <input type="time" name="sighted_time" required value="{{ values.sighted_time or '' }}"></label>
    </div>
    <label>Location (as text)
      <input name="location_text" required minlength="2" value="{{ values.location_text or '' }}"
             placeholder="e.g. Above the harbour, Victoria, BC">
    </label>
    <div class="row">
      <label>City (optional) <input name="city" value="{{ values.city or '' }}"></label>
      <label>Country (optional) <input name="country" value="{{ values.country or '' }}"></label>
    </div>
    <label>Pin the spot on the map (optional — drop the pin where the sighting happened, not where you live)
      <div id="pinmap" style="height:280px;border-radius:10px;border:1px solid var(--line)"></div>
    </label>
    <div class="row">
      <label>Shape
        <select name="shape">
          <option value="">Not sure</option>
          {% for s in shapes %}<option value="{{ s }}" {% if values.shape == s %}selected{% endif %}>{{ s | capitalize }}</option>{% endfor %}
        </select>
      </label>
      <label>Witnesses <input type="number" name="witnesses" min="1" max="1000" value="{{ values.witnesses or '' }}"></label>
    </div>
    <div class="row">
      <label>Duration <input type="number" name="duration_value" min="0" step="any" value="{{ values.duration_value or '' }}"></label>
      <label>Unit
        <select name="duration_unit">
          {% for u in ['seconds','minutes','hours'] %}<option value="{{ u }}" {% if values.duration_unit == u %}selected{% endif %}>{{ u }}</option>{% endfor %}
        </select>
      </label>
    </div>

    <label>Photos / video (up to {{ max_files }} files — images ≤ 25MB, video ≤ 500MB)</label>
    <div class="dropzone" id="dropzone">Drop files here or click to choose</div>
    <input type="file" id="filepick" multiple accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,video/webm" hidden>
    <div class="filelist" id="filelist"></div>

    <button class="btn primary" type="submit" id="submitbtn">Submit &amp; post to r/UFOs</button>
  </form>
</section>
{% endblock %}
{% block head %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script defer src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script defer src="/static/js/upload.js"></script>
{% endblock %}
```

- [ ] **Step 9: Create `static/js/upload.js`**

```javascript
// Direct-to-R2 uploads via presigned PUT + submit-form glue (timezone, map pin).
(function () {
  "use strict";

  // --- timezone default ---
  const tzInput = document.getElementById("tz_name");
  if (tzInput && !tzInput.value) {
    tzInput.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  }

  // --- optional map pin ---
  const pinDiv = document.getElementById("pinmap");
  if (pinDiv && window.L) {
    const latInput = document.getElementById("lat");
    const lonInput = document.getElementById("lon");
    const map = L.map("pinmap").setView(
      latInput.value ? [+latInput.value, +lonInput.value] : [30, 0],
      latInput.value ? 8 : 2
    );
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
    let marker = latInput.value
      ? L.marker([+latInput.value, +lonInput.value]).addTo(map)
      : null;
    map.on("click", (e) => {
      latInput.value = e.latlng.lat.toFixed(5);
      lonInput.value = e.latlng.lng.toFixed(5);
      if (marker) marker.setLatLng(e.latlng);
      else marker = L.marker(e.latlng).addTo(map);
    });
  }

  // --- uploads ---
  const dropzone = document.getElementById("dropzone");
  const filepick = document.getElementById("filepick");
  const filelist = document.getElementById("filelist");
  const mediaInput = document.getElementById("media_json");
  const submitBtn = document.getElementById("submitbtn");
  if (!dropzone) return;

  let media = [];
  try { media = JSON.parse(mediaInput.value) || []; } catch (e) { media = []; }
  let inflight = 0;
  media.forEach((m) => renderRow(m.key.split("/").pop(), m, null));

  function syncState() {
    mediaInput.value = JSON.stringify(media);
    submitBtn.disabled = inflight > 0;
    submitBtn.textContent = inflight > 0 ? "Uploading…" : "Submit & post to r/UFOs";
  }

  function renderRow(name, item, progressEl) {
    const row = document.createElement("div");
    row.className = "file";
    row.innerHTML = "<span>" + name + "</span>";
    if (progressEl) row.appendChild(progressEl);
    const rm = document.createElement("button");
    rm.type = "button"; rm.className = "btn danger"; rm.textContent = "remove";
    rm.onclick = () => {
      media = media.filter((m) => m !== item);
      row.remove();
      syncState();
    };
    row.appendChild(rm);
    filelist.appendChild(row);
    return row;
  }

  function putWithRetry(url, file, progress, attempt) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", file.type);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) progress.value = e.loaded / e.total;
      };
      xhr.onload = () =>
        xhr.status >= 200 && xhr.status < 300 ? resolve() : xhr.onerror();
      xhr.onerror = () => {
        if (attempt < 3) {
          setTimeout(() => putWithRetry(url, file, progress, attempt + 1).then(resolve, reject),
                     1000 * attempt);
        } else reject(new Error("upload failed after 3 attempts"));
      };
      xhr.send(file);
    });
  }

  async function uploadFile(file) {
    if (media.length + inflight >= parseInt("{{ max_files }}", 10) || media.length >= 10) {
      alert("Maximum number of files reached."); return;
    }
    const progress = document.createElement("progress");
    progress.max = 1; progress.value = 0;
    const item = { key: null, kind: null, width: null, height: null, size_bytes: file.size };
    const row = renderRow(file.name, item, progress);
    inflight++; syncState();
    try {
      const presign = await fetch("/api/presign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
      });
      if (!presign.ok) throw new Error((await presign.json()).detail || "presign failed");
      const info = await presign.json();
      await putWithRetry(info.upload_url, file, progress, 1);
      item.key = info.key; item.kind = info.kind;
      if (info.kind === "image") {
        await new Promise((done) => {
          const img = new Image();
          img.onload = () => { item.width = img.naturalWidth; item.height = img.naturalHeight; done(); };
          img.onerror = done;
          img.src = URL.createObjectURL(file);
        });
      }
      media.push(item);
      progress.remove();
    } catch (err) {
      row.innerHTML = "<span class='err'>" + file.name + " — " + err.message + "</span>";
    } finally {
      inflight--; syncState();
    }
  }

  dropzone.onclick = () => filepick.click();
  filepick.onchange = () => [...filepick.files].forEach(uploadFile);
  ["dragover", "dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.toggle("drag", ev === "dragover");
      if (ev === "drop") [...e.dataTransfer.files].forEach(uploadFile);
    })
  );
  syncState();
})();
```

Note: the `{{ max_files }}` placeholder inside upload.js is NOT rendered (static file) — the hard limit is enforced server-side; the client uses the literal `10` fallback in the same condition. Keep both checks as written.

- [ ] **Step 10: Run all submission tests**

Run: `.venv/bin/pytest tests/test_submit.py tests/test_helpers.py -v`
Expected: all passed (9 + 5).

- [ ] **Step 11: Run the full suite and commit**

Run: `.venv/bin/pytest -q` — all green.

```bash
git add app/helpers.py app/routes/submit.py app/web.py app/templates/submit.html static/js/upload.js tests/
git commit -m "feat: structured submission flow with post-as-user and drafts"
```

---

### Task 9: Thumbnail worker (Pillow images, ffmpeg video posters)

**Files:**
- Create: `app/thumbs.py`
- Test: `tests/test_thumbs.py`

**Interfaces:**
- Consumes: `r2.public_url/put_bytes`, `db.connect`, media rows where `thumb_key IS NULL AND thumb_attempts < 2`.
- Produces: `generate_image_thumb(data: bytes) -> bytes` (JPEG ≤ 640px long edge); `generate_video_poster(url: str) -> bytes` (ffmpeg frame grab over HTTP); `thumb_key_for(r2_key: str) -> str` (`uploads/Y/M/hash.ext` → `thumbs/Y/M/hash.jpg`); `process_pending(conn, limit: int = 3) -> int`; `start_worker(stop_event: threading.Event) -> threading.Thread` (daemon; used by `create_app`).

- [ ] **Step 1: Write the failing tests**

`tests/test_thumbs.py`:
```python
import io

from PIL import Image

from app import thumbs
from tests.test_db import _insert_sighting


def _png_bytes(w=1600, h=1200) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 40, 50)).save(buf, "PNG")
    return buf.getvalue()


def test_generate_image_thumb_shrinks_to_jpeg():
    out = thumbs.generate_image_thumb(_png_bytes())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) <= 640


def test_thumb_key_for():
    key = "uploads/2026/07/" + "a" * 32 + ".mp4"
    assert thumbs.thumb_key_for(key) == "thumbs/2026/07/" + "a" * 32 + ".jpg"


def test_process_pending_image(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, "uploads/2026/07/" + "b" * 32 + ".png"),
    )
    db_conn.commit()

    class FakeResp:
        content = _png_bytes()
        def raise_for_status(self): pass

    uploaded = {}
    monkeypatch.setattr(thumbs.httpx, "get", lambda url, timeout: FakeResp())
    monkeypatch.setattr(thumbs.r2, "put_bytes", lambda k, d, ct: uploaded.update({k: len(d)}))

    assert thumbs.process_pending(db_conn) == 1
    row = db_conn.execute("SELECT thumb_key, thumb_attempts FROM media").fetchone()
    assert row["thumb_key"] == "thumbs/2026/07/" + "b" * 32 + ".jpg"
    assert row["thumb_attempts"] == 1
    assert row["thumb_key"] in uploaded


def test_process_pending_video_uses_poster(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'video')",
        (sid, "uploads/2026/07/" + "c" * 32 + ".mp4"),
    )
    db_conn.commit()
    monkeypatch.setattr(thumbs, "generate_video_poster", lambda url: b"fake-jpeg")
    monkeypatch.setattr(thumbs.r2, "put_bytes", lambda k, d, ct: None)
    assert thumbs.process_pending(db_conn) == 1


def test_process_pending_gives_up_after_two_attempts(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, "uploads/2026/07/" + "d" * 32 + ".png"),
    )
    db_conn.commit()

    def boom(url, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr(thumbs.httpx, "get", boom)
    assert thumbs.process_pending(db_conn) == 0  # attempt 1
    assert thumbs.process_pending(db_conn) == 0  # attempt 2
    assert thumbs.process_pending(db_conn) == 0  # no more attempts
    row = db_conn.execute("SELECT thumb_key, thumb_attempts FROM media").fetchone()
    assert row["thumb_key"] is None
    assert row["thumb_attempts"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_thumbs.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'app.thumbs'`.

- [ ] **Step 3: Implement `app/thumbs.py`**

```python
import io
import subprocess
import threading

import httpx
from PIL import Image, ImageOps

from app import db, r2
from app.config import get_settings

THUMB_MAX = 640


def generate_image_thumb(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_MAX, THUMB_MAX))
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


def generate_video_poster(url: str) -> bytes:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", "1", "-i", url, "-frames:v", "1",
        "-vf", "scale='min(640,iw)':-2",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg poster failed: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout


def thumb_key_for(r2_key: str) -> str:
    rest = r2_key.split("/", 1)[1]
    return "thumbs/" + rest.rsplit(".", 1)[0] + ".jpg"


def process_pending(conn, limit: int = 3) -> int:
    rows = conn.execute(
        """SELECT id, r2_key, kind FROM media
           WHERE thumb_key IS NULL AND thumb_attempts < 2
           ORDER BY id LIMIT ?""",
        (limit,),
    ).fetchall()
    done = 0
    for row in rows:
        conn.execute("UPDATE media SET thumb_attempts = thumb_attempts + 1 WHERE id=?", (row["id"],))
        conn.commit()
        try:
            url = r2.public_url(row["r2_key"])
            if row["kind"] == "image":
                resp = httpx.get(url, timeout=60)
                resp.raise_for_status()
                thumb = generate_image_thumb(resp.content)
            else:
                thumb = generate_video_poster(url)
            tkey = thumb_key_for(row["r2_key"])
            r2.put_bytes(tkey, thumb, "image/jpeg")
            conn.execute("UPDATE media SET thumb_key=? WHERE id=?", (tkey, row["id"]))
            conn.commit()
            done += 1
        except Exception as exc:
            print(f"thumbs: media {row['id']} failed: {exc}")
    return done


def start_worker(stop_event: threading.Event) -> threading.Thread:
    def run():
        conn = db.connect(get_settings().db_path)
        while not stop_event.is_set():
            try:
                if process_pending(conn) == 0:
                    stop_event.wait(10)
            except Exception as exc:
                print(f"thumbs: worker error: {exc}")
                stop_event.wait(30)
        conn.close()

    thread = threading.Thread(target=run, name="thumb-worker", daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_thumbs.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/thumbs.py tests/test_thumbs.py
git commit -m "feat: thumbnail worker (pillow images, ffmpeg video posters)"
```
