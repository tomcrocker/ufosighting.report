# Anonymous Submission + Verify + Bot Posting + Ingest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the OAuth-gated submission with an anonymous flow: user enters their Reddit username, `ufosightingsbot` DMs a verify link; clicking it posts the sighting instantly, otherwise it falls to a mod-review queue. Add auto-ingest of Sighting-flaired posts.

**Architecture:** Same FastAPI + Jinja2 + SQLite(WAL/FTS5) app on the Oracle VM. Submission drops the login requirement and gains Cloudflare Turnstile, a required manual Reddit username, per-IP rate limiting, and a `pending_verify` → (verify link | 6h fallback → mod queue) → bot-posts-`live` lifecycle. A shared `ufosightingsbot` script app (the `SCRIPT_*` creds) sends the verify DM, posts approved/verified sightings, runs the moderation sync, and drives the ingest poller. The existing OAuth code stays mounted but dormant.

**Tech Stack:** Python 3.12, FastAPI, httpx, boto3, Pillow + ffmpeg, SQLite, flatpickr (CDN), Cloudflare Turnstile, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-07-11-anon-submission-bot-ingest-design.md`

## Global Constraints

- Reuse existing modules/patterns (`app/reddit.py`, `app/helpers.py`, `app/db.py`, `app/routes/*`). Don't rewrite what works.
- Statuses (TEXT column): `pending_verify`, `pending_review`, `live`, `rejected`, `removed_on_reddit`, `deleted_by_user`, `hidden_by_admin`. Only `live` is public. `hidden_by_admin` never auto-changes.
- Verify tokens: `secrets.token_urlsafe(32)`, single-use (cleared on use), unguessable. Never a sequential id.
- Attribution on Reddit posts is plain text, no `u/`-mention that pings: verified → "Reported by u/NAME (verified via ufosighting.report)"; mod-approved → "(self-reported via ufosighting.report)".
- OAuth code (`app/auth.py` sessions, `app/reddit_oauth.py`, `app/routes/auth.py`) stays — do not delete. `/submit` no longer requires it.
- Media bytes never pass through the app except thumbnails + ingest downloads. Presigned PUT stays.
- `SCRIPT_*` env = `ufosightingsbot`'s script app + account; drives DM, posting, sync, ingest.
- Turnstile: if `TURNSTILE_SECRET_KEY` is empty (dev), verification is bypassed (returns ok) so local/dev works without keys.
- Run tests: `.venv/bin/pytest -q` from repo root. Commit at the end of every task.

## File Structure

```
app/
  config.py       # + turnstile keys, rate limits, verify window/dm-guard
  db.py           # + new columns, idempotent ADD COLUMN migration, rate_events table
  turnstile.py    # NEW verify_turnstile(token, ip) -> bool
  ratelimit.py    # NEW record()/allowed() DB-backed per-ip/action counter
  reddit.py       # + send_message() (compose DM), list_sighting_posts(), fetch_post()
  posting.py      # NEW post_sighting_to_reddit(conn, sighting_id, verified) shared by verify + approve
  verify.py       # NEW new_token(), verify_message_text(), sweep_pending_verify()
  helpers.py      # format_post_body() gains attribution line; username validation
  routes/
    submit.py     # anon: no login, username, turnstile, ratelimit, pending_verify, fire DM
    verify.py     # NEW GET /verify/{token}
    admin.py      # + /admin/review queue + approve/reject
    public.py     # /api/geocode: no login + ratelimit
  templates/
    submit.html   # flatpickr, username field, turnstile widget, csrf double-submit
    submitted.html# NEW "check your inbox" page
    verify_result.html # NEW verify outcome page
    review.html   # NEW mod review queue
    admin.html    # + link to review + pending count
  static/js/wizard.js  # flatpickr init
ingest.py         # NEW Sighting-post poller + --backfill
sync.py           # + sweep_pending_verify() call
cleanup.py        # + prune old rate_events
deploy/           # + ufosighting-ingest.service/.timer; .env.example; RUNBOOK
tests/            # test_ratelimit, test_turnstile, test_verify, test_posting,
                  # test_submit (rewritten), test_verify_routes, test_review, test_ingest
```

---

### Task 1: Config + schema migration + rate_events table

**Files:**
- Modify: `app/config.py`, `app/db.py`, `.env.example`
- Test: `tests/test_config.py` (extend), `tests/test_db.py` (extend)

**Interfaces:**
- Produces (`Settings` new fields): `turnstile_site_key: str`, `turnstile_secret_key: str`, `rate_submit_per_hour: int (5)`, `rate_presign_per_hour: int (40)`, `rate_geocode_per_hour: int (60)`, `verify_window_hours: int (6)`, `verify_dm_per_username_hours: int (1)`.
- Produces (schema): `sightings` gains `submitter_ip TEXT`, `username_verified INTEGER NOT NULL DEFAULT 0`, `verify_token TEXT`, `verify_sent_at TEXT`; index `idx_sightings_verify_token`; new table `rate_events(id, ip, action, created_at)`. `init_db` runs an idempotent migration adding the four columns to a pre-existing `sightings` table.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:
```python
def test_new_settings_defaults():
    s = get_settings()
    assert s.rate_submit_per_hour == 5
    assert s.rate_presign_per_hour == 40
    assert s.rate_geocode_per_hour == 60
    assert s.verify_window_hours == 6
    assert s.verify_dm_per_username_hours == 1
    assert s.turnstile_site_key == ""       # unset in test env
    assert s.turnstile_secret_key == ""
```

Add to `tests/test_db.py`:
```python
def test_new_columns_exist(db_conn):
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(sightings)")}
    assert {"submitter_ip", "username_verified", "verify_token", "verify_sent_at"} <= cols


def test_verify_defaults(db_conn):
    sid = _insert_sighting(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["username_verified"] == 0
    assert row["verify_token"] is None


def test_rate_events_table(db_conn):
    db_conn.execute("INSERT INTO rate_events (ip, action) VALUES ('1.2.3.4','submit')")
    db_conn.commit()
    n = db_conn.execute("SELECT COUNT(*) FROM rate_events WHERE ip='1.2.3.4'").fetchone()[0]
    assert n == 1


def test_migration_adds_columns_to_legacy_table(tmp_path):
    import sqlite3
    from app import db
    p = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(p)
    raw.execute("""CREATE TABLE sightings (id INTEGER PRIMARY KEY, reddit_username TEXT,
                   title TEXT, description TEXT, sighted_at TEXT, location_text TEXT,
                   status TEXT DEFAULT 'pending_post')""")
    raw.commit(); raw.close()
    conn = db.connect(p)
    db.init_db(conn)  # must ALTER, not crash
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    assert "verify_token" in cols and "submitter_ip" in cols
    conn.close()
```

- [ ] **Step 2: Run — expect fail**

`.venv/bin/pytest tests/test_db.py::test_new_columns_exist tests/test_config.py::test_new_settings_defaults -q` → FAIL.

- [ ] **Step 3: Implement config**

In `app/config.py` add fields to the `Settings` dataclass (after `max_files`):
```python
    turnstile_site_key: str
    turnstile_secret_key: str
    rate_submit_per_hour: int
    rate_presign_per_hour: int
    rate_geocode_per_hour: int
    verify_window_hours: int
    verify_dm_per_username_hours: int
```
And in `get_settings()` return (after `max_files=10,`):
```python
        turnstile_site_key=_env("TURNSTILE_SITE_KEY", ""),
        turnstile_secret_key=_env("TURNSTILE_SECRET_KEY", ""),
        rate_submit_per_hour=int(_env("RATE_SUBMIT_PER_HOUR", "5")),
        rate_presign_per_hour=int(_env("RATE_PRESIGN_PER_HOUR", "40")),
        rate_geocode_per_hour=int(_env("RATE_GEOCODE_PER_HOUR", "60")),
        verify_window_hours=int(_env("VERIFY_WINDOW_HOURS", "6")),
        verify_dm_per_username_hours=int(_env("VERIFY_DM_PER_USERNAME_HOURS", "1")),
```

- [ ] **Step 4: Implement schema + migration**

In `app/db.py` SCHEMA, add the four columns to the `sightings` CREATE (after `location_obscured` line):
```sql
  submitter_ip TEXT,
  username_verified INTEGER NOT NULL DEFAULT 0,
  verify_token TEXT,
  verify_sent_at TEXT,
```
Add after the sightings indexes:
```sql
CREATE INDEX IF NOT EXISTS idx_sightings_verify_token ON sightings(verify_token);
```
Add a new table near `sessions`:
```sql
CREATE TABLE IF NOT EXISTS rate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ip TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_rate_events_lookup ON rate_events(ip, action, created_at);
```
Replace `init_db` with a version that runs `executescript(SCHEMA)` then the idempotent migration:
```python
_MIGRATION_COLUMNS = [
    ("submitter_ip", "TEXT"),
    ("username_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("verify_token", "TEXT"),
    ("verify_sent_at", "TEXT"),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE sightings ADD COLUMN {name} {decl}")
    conn.commit()
```

- [ ] **Step 5: Update `.env.example`** — append the Turnstile + rate + verify vars from the spec's config section.

- [ ] **Step 6: Run — expect pass**

`.venv/bin/pytest tests/test_db.py tests/test_config.py -q` → all pass.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/db.py .env.example tests/
git commit -m "feat: verify/rate schema columns, rate_events table, idempotent migration"
```

---

### Task 2: Rate limiter

**Files:**
- Create: `app/ratelimit.py`
- Test: `tests/test_ratelimit.py`

**Interfaces:**
- Produces: `record(conn, ip, action) -> None`; `allowed(conn, ip, action, limit, window_hours=1) -> bool` (True if the count of `action` events for `ip` in the last `window_hours` is `< limit`); `count_recent(conn, ip, action, window_hours) -> int`.

- [ ] **Step 1: Failing tests** — `tests/test_ratelimit.py`:
```python
from app import ratelimit


def test_allowed_until_limit(db_conn):
    for _ in range(3):
        assert ratelimit.allowed(db_conn, "1.1.1.1", "submit", limit=3)
        ratelimit.record(db_conn, "1.1.1.1", "submit")
    assert not ratelimit.allowed(db_conn, "1.1.1.1", "submit", limit=3)


def test_separate_ips_and_actions(db_conn):
    ratelimit.record(db_conn, "1.1.1.1", "submit")
    assert ratelimit.count_recent(db_conn, "2.2.2.2", "submit", 1) == 0
    assert ratelimit.count_recent(db_conn, "1.1.1.1", "presign", 1) == 0
    assert ratelimit.count_recent(db_conn, "1.1.1.1", "submit", 1) == 1


def test_window_excludes_old(db_conn):
    db_conn.execute(
        "INSERT INTO rate_events (ip, action, created_at) "
        "VALUES ('1.1.1.1','submit', strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours'))"
    )
    db_conn.commit()
    assert ratelimit.count_recent(db_conn, "1.1.1.1", "submit", 1) == 0
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `app/ratelimit.py`**:
```python
import sqlite3


def record(conn: sqlite3.Connection, ip: str, action: str) -> None:
    conn.execute("INSERT INTO rate_events (ip, action) VALUES (?,?)", (ip, action))
    conn.commit()


def count_recent(conn: sqlite3.Connection, ip: str, action: str, window_hours: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM rate_events
           WHERE ip=? AND action=?
             AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (ip, action, f"-{window_hours} hours"),
    ).fetchone()[0]


def allowed(conn, ip: str, action: str, limit: int, window_hours: int = 1) -> bool:
    return count_recent(conn, ip, action, window_hours) < limit
```

- [ ] **Step 4: Run — expect pass. Step 5: Commit**
```bash
git add app/ratelimit.py tests/test_ratelimit.py
git commit -m "feat: DB-backed per-IP/action rate limiter"
```

---

### Task 3: Turnstile verifier

**Files:**
- Create: `app/turnstile.py`
- Test: `tests/test_turnstile.py`

**Interfaces:**
- Produces: `verify(token: str, remote_ip: str | None = None) -> bool`. If `settings.turnstile_secret_key` is empty → return True (dev bypass). Otherwise POST to `https://challenges.cloudflare.com/turnstile/v0/siteverify` and return the `success` bool. Network error → False.

- [ ] **Step 1: Failing tests** — `tests/test_turnstile.py`:
```python
import httpx, respx, pytest
from app import turnstile
from app.config import get_settings

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def test_dev_bypass_when_no_secret():
    assert turnstile.verify("anything") is True  # test env has empty secret


@respx.mock
def test_success(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "sekret")
    get_settings.cache_clear()
    respx.post(SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    assert turnstile.verify("good-token", "1.2.3.4") is True


@respx.mock
def test_failure(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "sekret")
    get_settings.cache_clear()
    respx.post(SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": False}))
    assert turnstile.verify("bad-token") is False


@respx.mock
def test_network_error_is_false(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "sekret")
    get_settings.cache_clear()
    respx.post(SITEVERIFY).mock(side_effect=httpx.ConnectError("down"))
    assert turnstile.verify("tok") is False
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `app/turnstile.py`**:
```python
import httpx
from app.config import get_settings

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify(token: str, remote_ip: str | None = None) -> bool:
    secret = get_settings().turnstile_secret_key
    if not secret:
        return True  # dev bypass when unconfigured
    data = {"secret": secret, "response": token or ""}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        resp = httpx.post(SITEVERIFY, data=data, timeout=10)
        return bool(resp.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False
```

- [ ] **Step 4: Run — expect pass. Step 5: Commit**
```bash
git add app/turnstile.py tests/test_turnstile.py
git commit -m "feat: Cloudflare Turnstile verifier with dev bypass"
```

---

### Task 4: Reddit DM + post-listing helpers

**Files:**
- Modify: `app/reddit.py`
- Test: `tests/test_reddit.py` (extend)

**Interfaces:**
- Consumes: `script_token()`, `_headers()`, `get_settings()`.
- Produces: `send_message(token, *, to, subject, text) -> None` (POST `/api/compose`, raise `RateLimited`/`RedditError` on errors); `list_flair_posts(token, *, subreddit, flair, limit=100, after=None) -> tuple[list[dict], str | None]` (GET subreddit search by flair, returns raw child `data` dicts + `after` cursor); `fetch_post(token, post_id) -> dict | None` (single post via `/api/info`).

- [ ] **Step 1: Failing tests** — add to `tests/test_reddit.py`:
```python
@respx.mock
def test_send_message_ok():
    route = respx.post("https://oauth.reddit.com/api/compose").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}})
    )
    reddit.send_message("tok", to="witness1", subject="Verify", text="link")
    body = route.calls[0].request.content
    assert b"to=witness1" in body and b"api_type=json" in body


@respx.mock
def test_send_message_ratelimit():
    respx.post("https://oauth.reddit.com/api/compose").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "try again in 3 minutes", "ratelimit"]]}})
    )
    with pytest.raises(reddit.RateLimited):
        reddit.send_message("tok", to="x", subject="s", text="t")


@respx.mock
def test_list_flair_posts_parses():
    respx.get("https://oauth.reddit.com/r/UFOs_sandbox/search").mock(
        return_value=httpx.Response(200, json={"data": {"after": "t3_next", "children": [
            {"data": {"id": "aaa", "title": "Orb", "author": "u1", "link_flair_text": "Sighting"}}]}})
    )
    posts, after = reddit.list_flair_posts("tok", subreddit="UFOs_sandbox", flair="Sighting")
    assert posts[0]["id"] == "aaa" and after == "t3_next"
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement in `app/reddit.py`** — add constants + functions:
```python
COMPOSE_URL = "https://oauth.reddit.com/api/compose"


def send_message(access_token: str, *, to: str, subject: str, text: str) -> None:
    resp = httpx.post(
        COMPOSE_URL,
        data={"api_type": "json", "to": to, "subject": subject[:100], "text": text},
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code == 401:
        raise TokenExpired("bot session expired")
    if resp.status_code != 200:
        raise RedditError(f"compose failed: HTTP {resp.status_code}")
    errors = (resp.json().get("json", {}) or {}).get("errors") or []
    if errors:
        code = errors[0][0]
        msg = errors[0][1] if len(errors[0]) > 1 else code
        if code == "RATELIMIT":
            raise RateLimited(msg)
        raise RedditError(f"{code}: {msg}")


def list_flair_posts(access_token, *, subreddit, flair, limit=100, after=None):
    params = {
        "q": f'flair_name:"{flair}"',
        "restrict_sr": 1, "sort": "new", "limit": limit, "type": "link",
    }
    if after:
        params["after"] = after
    resp = httpx.get(
        f"https://oauth.reddit.com/r/{subreddit}/search",
        params=params, headers=_headers(access_token), timeout=30,
    )
    if resp.status_code != 200:
        raise RedditError(f"listing failed: HTTP {resp.status_code}")
    data = resp.json().get("data", {})
    return [c["data"] for c in data.get("children", [])], data.get("after")


def fetch_post(access_token, post_id):
    resp = httpx.get(
        INFO_URL, params={"id": "t3_" + post_id},
        headers=_headers(access_token), timeout=20,
    )
    if resp.status_code != 200:
        return None
    children = resp.json().get("data", {}).get("children", [])
    return children[0]["data"] if children else None
```

- [ ] **Step 4: Run — expect pass. Step 5: Commit**
```bash
git add app/reddit.py tests/test_reddit.py
git commit -m "feat: reddit DM compose + flair listing + single-post fetch"
```

---

### Task 5: Verify module + attribution helper + shared posting

**Files:**
- Create: `app/verify.py`, `app/posting.py`
- Modify: `app/helpers.py`
- Test: `tests/test_verify.py`, `tests/test_posting.py`, `tests/test_helpers.py` (extend)

**Interfaces:**
- Produces (`app/helpers.py`): `clean_username(raw) -> str | None` (strip leading `u/` or `/u/`, validate `^[A-Za-z0-9_-]{3,20}$`, else None); `format_post_body(..., attribution: str)` gains a keyword `attribution` inserted as a "Reported by …" line above the gallery link.
- Produces (`app/verify.py`): `new_token() -> str` (`secrets.token_urlsafe(32)`); `verify_message(username, verify_url) -> tuple[str, str]` (subject, text); `sweep_pending_verify(conn, window_hours) -> int` (pending_verify older than window → pending_review, returns count).
- Produces (`app/posting.py`): `post_sighting(conn, sighting_id, *, verified: bool) -> str` — loads the sighting, builds body via `helpers.format_post_body` with the right attribution, calls `reddit.submit_post(reddit.script_token(), ...)`, on success sets `reddit_post_id` + `status='live'` (+ `username_verified=1` if verified) + clears `verify_token`, returns post id. Propagates `reddit.RateLimited`/`RedditError` without changing status.

- [ ] **Step 1: Failing tests**

`tests/test_helpers.py` add:
```python
def test_clean_username():
    assert helpers.clean_username("u/Example_1") == "Example_1"
    assert helpers.clean_username("/u/tmosh") == "tmosh"
    assert helpers.clean_username("  Witness-9 ") == "Witness-9"
    assert helpers.clean_username("ab") is None            # too short
    assert helpers.clean_username("has space") is None
    assert helpers.clean_username("bad!char") is None


def test_format_post_body_attribution():
    body = helpers.format_post_body(
        {"tz_name": "UTC", "description": "d"},
        sighted_local="2026-07-01 22:15", location_line="",
        media_urls=[], gallery_url="https://x/1",
        attribution="Reported by u/witness1 (verified via ufosighting.report)",
    )
    assert "Reported by u/witness1 (verified via ufosighting.report)" in body
```

`tests/test_verify.py`:
```python
from app import verify
from tests.test_db import _insert_sighting


def test_new_token_unguessable():
    a, b = verify.new_token(), verify.new_token()
    assert a != b and len(a) >= 32


def test_verify_message_contains_url():
    subject, text = verify.verify_message("witness1", "https://ufosighting.report/verify/abc")
    assert "ufosighting.report/verify/abc" in text
    assert "ignore" in text.lower()


def test_sweep_moves_stale_pending_verify(db_conn):
    old = _insert_sighting(db_conn)
    db_conn.execute(
        "UPDATE sightings SET status='pending_verify', "
        "verify_sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 hours') WHERE id=?", (old,))
    fresh = _insert_sighting(db_conn)
    db_conn.execute(
        "UPDATE sightings SET status='pending_verify', "
        "verify_sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hours') WHERE id=?", (fresh,))
    db_conn.commit()
    moved = verify.sweep_pending_verify(db_conn, window_hours=6)
    assert moved == 1
    assert db_conn.execute("SELECT status FROM sightings WHERE id=?", (old,)).fetchone()[0] == "pending_review"
    assert db_conn.execute("SELECT status FROM sightings WHERE id=?", (fresh,)).fetchone()[0] == "pending_verify"
```

`tests/test_posting.py`:
```python
import httpx, respx
from app import posting
from tests.test_db import _insert_sighting


def _seed_ready(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("UPDATE sightings SET status='pending_verify', reddit_username='witness1', "
                    "verify_token='tok123' WHERE id=?", (sid,))
    db_conn.commit()
    return sid


@respx.mock
def test_post_sighting_verified(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_zzz"}}}))
    sid = _seed_ready(db_conn)
    pid = posting.post_sighting(db_conn, sid, verified=True)
    assert pid == "zzz"
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["reddit_post_id"] == "zzz"
    assert row["username_verified"] == 1 and row["verify_token"] is None
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `helpers.clean_username` + attribution**

Add to `app/helpers.py`:
```python
import re as _re
USERNAME_RE = _re.compile(r"^[A-Za-z0-9_-]{3,20}$")


def clean_username(raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    for prefix in ("/u/", "u/", "/U/", "U/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name if USERNAME_RE.fullmatch(name) else None
```
Change `format_post_body` signature to accept `attribution: str = ""` and insert it just before the gallery-link line:
```python
    if attribution:
        parts.append(attribution)
    parts.append(
        f"[View this sighting in the gallery]({gallery_url}) — "
        f"*submitted via [ufosighting.report](https://ufosighting.report)*"
    )
```
(Add `attribution: str = ""` to the keyword args of `format_post_body`.)

- [ ] **Step 4: Implement `app/verify.py`**:
```python
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def verify_message(username: str, verify_url: str) -> tuple[str, str]:
    subject = "Confirm your UFO sighting submission"
    text = (
        f"Hi u/{username},\n\n"
        f"Did you just submit a UFO sighting on ufosighting.report? "
        f"Confirm it was you and it will be posted right away:\n\n{verify_url}\n\n"
        f"If this wasn't you, you can safely ignore this message — nothing will be posted."
    )
    return subject, text


def sweep_pending_verify(conn, window_hours: int) -> int:
    cur = conn.execute(
        """UPDATE sightings SET status='pending_review'
           WHERE status='pending_verify'
             AND verify_sent_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (f"-{window_hours} hours",),
    )
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 5: Implement `app/posting.py`**:
```python
import json
from app import helpers, r2, reddit
from app.config import get_settings


def post_sighting(conn, sighting_id: int, *, verified: bool) -> str:
    s = get_settings()
    row = conn.execute("SELECT * FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    clean = dict(row)
    for f in ("movement", "sensors", "witness_background"):
        clean[f] = json.loads(row[f]) if row[f] else []
    media = conn.execute(
        "SELECT r2_key FROM media WHERE sighting_id=? ORDER BY sort_order", (sighting_id,)
    ).fetchall()
    slug = helpers.slugify(row["title"])
    gallery_url = f"{s.base_url}/sighting/{sighting_id}/{slug}"
    location_line = ", ".join(dict.fromkeys(
        p for p in (row["location_text"], row["city"], row["country"]) if p))
    tag = "verified" if verified else "self-reported"
    attribution = f"Reported by u/{row['reddit_username']} ({tag} via ufosighting.report)"
    body = helpers.format_post_body(
        clean, sighted_local=helpers.from_utc(row["sighted_at"], row["tz_name"]),
        location_line=location_line,
        media_urls=[r2.public_url(m["r2_key"]) for m in media],
        gallery_url=gallery_url, attribution=attribution,
    )
    post_id = reddit.submit_post(
        reddit.script_token(), subreddit=s.subreddit,
        title=row["title"], body=body, flair_id=s.sighting_flair_id,
    )
    conn.execute(
        "UPDATE sightings SET reddit_post_id=?, status='live', username_verified=?, "
        "verify_token=NULL WHERE id=?",
        (post_id, 1 if verified else row["username_verified"], sighting_id),
    )
    conn.commit()
    return post_id
```

- [ ] **Step 6: Run — expect pass** (`tests/test_verify.py test_posting.py test_helpers.py`).

- [ ] **Step 7: Commit**
```bash
git add app/verify.py app/posting.py app/helpers.py tests/
git commit -m "feat: verify tokens/sweep, shared bot posting, username + attribution helpers"
```

---

### Task 6: Rework submission flow (anonymous, verify DM)

**Files:**
- Modify: `app/routes/submit.py`, `app/routes/public.py` (geocode), `app/web.py` (anon CSRF helper), `tests/conftest.py`, `tests/test_submit.py` (rewrite), `tests/test_presign.py` (adjust)
- Create: `app/templates/submitted.html`

**Interfaces:**
- Consumes: `turnstile.verify`, `ratelimit.*`, `verify.new_token/verify_message`, `reddit.send_message/script_token`, `helpers.clean_username`, `r2.head_exists`.
- Produces: anon `GET/POST /submit`, `POST /api/presign`, `GET /api/geocode` (no login). CSRF via double-submit cookie `csrf` (random per GET). `POST /submit` → `pending_verify` + fire DM → renders `submitted.html`. New form field `reddit_username`. `client_ip(request)` helper (honors `X-Forwarded-For`).

- [ ] **Step 1: Rewrite tests** — replace `tests/test_submit.py` login-based cases with anon ones:
```python
import json, httpx, respx, pytest
from app import verify as verifymod

MEDIA_KEY = "uploads/2026/07/" + "a" * 32 + ".jpg"
STORY = ("A silent amber orb hovered above the treeline for two minutes, pulsing softly, "
         "then shot straight up and vanished. No sound, clear sky, three of us watched it.")


def form(csrf):
    return {
        "csrf_token": csrf, "cf-turnstile-response": "x",
        "reddit_username": "witness1",
        "title": "Amber orb over the lake", "description": STORY,
        "sighted_date": "2026-07-01", "sighted_time": "22:15", "tz_name": "America/Vancouver",
        "location_text": "Lake Cowichan, BC", "city": "Lake Cowichan", "country": "Canada",
        "lat": "48.82", "lon": "-124.05", "location_obscured": "",
        "duration_value": "120", "duration_unit": "seconds", "witnesses": "2",
        "shape": "sphere", "num_objects": "1", "distance": "above the trees",
        "apparent_size": "dime", "movement_json": json.dumps(["hovering"]),
        "sensors_json": "[]", "background_json": "[]",
        "has_wings": "", "has_rotors": "", "has_plume": "", "makes_noise": "",
        "media_json": json.dumps([{"key": MEDIA_KEY, "kind": "image", "width": 100,
                                   "height": 80, "size_bytes": 1234}]),
    }


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr("app.routes.submit.r2.head_exists", lambda k: True)
    monkeypatch.setattr("app.routes.submit.turnstile.verify", lambda t, ip=None: True)
    from app.routes import submit as sm
    sm._geocode_cache.clear()


def get_csrf(client):
    r = client.get("/submit")
    assert r.status_code == 200
    return client.cookies["csrf"]


def test_submit_anonymous_reaches_wizard(client):
    r = client.get("/submit")
    assert r.status_code == 200
    assert 'name="reddit_username"' in r.text
    assert "csrf" in client.cookies


@respx.mock
def test_happy_path_creates_pending_verify_and_dms(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.submit.reddit.script_token", lambda: "bot-tok")
    dm = respx.post("https://oauth.reddit.com/api/compose").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}}))
    csrf = get_csrf(client)
    r = client.post("/submit", data=form(csrf), follow_redirects=False)
    assert r.status_code == 200 and "inbox" in r.text.lower()
    row = app_db.execute("SELECT * FROM sightings WHERE id=1").fetchone()
    assert row["status"] == "pending_verify"
    assert row["reddit_username"] == "witness1"
    assert row["verify_token"] and row["reddit_post_id"] is None
    sent = dm.calls[0].request.content
    assert b"to=witness1" in sent and b"verify" in sent.lower()


def test_bad_username_rejected(client, app_db):
    csrf = get_csrf(client)
    f = form(csrf); f["reddit_username"] = "no"
    r = client.post("/submit", data=f)
    assert r.status_code == 422 and app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_bad_csrf_rejected(client):
    get_csrf(client)
    f = form("forged")
    assert client.post("/submit", data=f).status_code == 403


def test_turnstile_failure_rejected(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.submit.turnstile.verify", lambda t, ip=None: False)
    csrf = get_csrf(client)
    r = client.post("/submit", data=form(csrf))
    assert r.status_code == 400 and app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_rate_limit_trips(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.submit.reddit.script_token", lambda: "t")
    monkeypatch.setattr("app.routes.submit.reddit.send_message", lambda *a, **k: None)
    csrf = get_csrf(client)
    for _ in range(5):
        client.post("/submit", data=form(csrf))
    r = client.post("/submit", data=form(csrf))
    assert r.status_code == 429


def test_geocode_no_login(client):
    # anon can now reach geocode (short query returns empty without network)
    assert client.get("/api/geocode?q=ab").json() == {"results": []}


def test_presign_no_login(client):
    r = client.post("/api/presign", json={"filename": "a.jpg", "content_type": "image/jpeg",
                                          "size_bytes": 1000})
    assert r.status_code == 200
```

Update `tests/test_presign.py`: delete `test_presign_requires_login`; keep the rest but they no longer need `logged_in` — use `client`.

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Add `client_ip` + anon CSRF to `app/web.py`**:
```python
import secrets

def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "0.0.0.0")

def new_csrf() -> str:
    return secrets.token_urlsafe(24)
```

- [ ] **Step 4: Rewrite `app/routes/submit.py`** — remove login gates, add anon flow. Key changes:
  - Imports add: `from app import helpers, r2, reddit, turnstile, verify, ratelimit`, `from app.web import templates, client_ip, new_csrf`, `from app.config import get_settings`. Remove `current_user` import.
  - `presign`: drop the `user is None` check; add rate limit:
```python
@router.post("/api/presign")
def presign(req: PresignRequest, request: Request, conn=Depends(db.get_db)):
    ip = client_ip(request)
    s = get_settings()
    if not ratelimit.allowed(conn, ip, "presign", s.rate_presign_per_hour):
        raise HTTPException(status_code=429, detail="Too many uploads, try again later")
    # ... existing type/size checks unchanged ...
    ratelimit.record(conn, ip, "presign")
    key = r2.make_upload_key(req.content_type)
    return {...}  # unchanged
```
  - `geocode` moves here or stays in submit.py; drop `user` param, add rate limit keyed `geocode` (return `{"results": []}` for short queries before limiting).
  - `GET /submit`: always render the wizard (no login page); set csrf cookie:
```python
@router.get("/submit")
def submit_form(request: Request, conn=Depends(db.get_db)):
    csrf = request.cookies.get("csrf") or new_csrf()
    resp = _render_form(request, values={}, errors=[], csrf=csrf)
    resp.set_cookie("csrf", csrf, max_age=7200, httponly=True, samesite="lax")
    return resp
```
  - `_render_form` no longer takes `user`; passes `csrf`, `turnstile_site_key`, `opts`, `values`, `errors`, `show_all`. Templates use `values.reddit_username` etc.
  - `POST /submit`:
```python
@router.post("/submit")
async def submit_create(request: Request, conn=Depends(db.get_db)):
    s = get_settings(); ip = client_ip(request)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    cookie_csrf = request.cookies.get("csrf", "")
    if not cookie_csrf or not hmac.compare_digest(form.get("csrf_token", ""), cookie_csrf):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    if not turnstile.verify(form.get("cf-turnstile-response", ""), ip):
        return _render_form(request, form, ["Anti-spam check failed — please try again."],
                            csrf=cookie_csrf, status_code=400)
    if not ratelimit.allowed(conn, ip, "submit", s.rate_submit_per_hour):
        return _render_form(request, form, ["You've submitted several sightings recently. "
                            "Please try again later."], csrf=cookie_csrf, status_code=429)
    username = helpers.clean_username(form.get("reddit_username"))
    clean, errors = validate_submission(form)
    if username is None:
        errors.insert(0, "Enter a valid Reddit username (3–20 letters, digits, _ or -).")
    for m in clean["media"]:
        if not r2.head_exists(m["key"]):
            errors.append("An uploaded file was not found — please re-upload."); break
    if errors:
        return _render_form(request, form, errors, csrf=cookie_csrf, status_code=422)

    token = verify.new_token()
    cur = conn.execute(
        """INSERT INTO sightings (reddit_username, title, description, sighted_at, tz_name,
             duration_seconds, shape, witnesses, num_objects, distance, apparent_size, movement,
             has_wings, has_rotors, has_plume, makes_noise, sensors, witness_background,
             location_text, city, country, lat, lon, location_obscured,
             submitter_ip, verify_token, verify_sent_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   strftime('%Y-%m-%dT%H:%M:%SZ','now'),'pending_verify')""",
        (username, clean["title"], clean["description"], clean["sighted_at"], clean["tz_name"],
         clean["duration_seconds"], clean["shape"], clean["witnesses"], clean["num_objects"],
         clean["distance"], clean["apparent_size"],
         json.dumps(clean["movement"]) if clean["movement"] else None,
         clean["has_wings"], clean["has_rotors"], clean["has_plume"], clean["makes_noise"],
         json.dumps(clean["sensors"]) if clean["sensors"] else None,
         json.dumps(clean["witness_background"]) if clean["witness_background"] else None,
         clean["location_text"], clean["city"], clean["country"], clean["lat"], clean["lon"],
         clean["location_obscured"], ip, token),
    )
    sid = cur.lastrowid
    for i, m in enumerate(clean["media"]):
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind, width, height, size_bytes, "
                     "sort_order) VALUES (?,?,?,?,?,?,?)",
                     (sid, m["key"], m["kind"], m["width"], m["height"], m["size_bytes"], i))
    conn.commit()
    ratelimit.record(conn, ip, "submit")
    _try_send_verify_dm(conn, username, token)
    return templates.TemplateResponse(request, "submitted.html", {"username": username})
```
  - Helper `_try_send_verify_dm` (per-username guard, non-fatal):
```python
def _try_send_verify_dm(conn, username, token):
    s = get_settings()
    if not ratelimit.allowed(conn, username.lower(), "dm", 1,
                             window_hours=s.verify_dm_per_username_hours):
        return
    verify_url = f"{s.base_url}/verify/{token}"
    subject, text = verify.verify_message(username, verify_url)
    try:
        reddit.send_message(reddit.script_token(), to=username, subject=subject, text=text)
        ratelimit.record(conn, username.lower(), "dm")
    except reddit.RedditError as exc:
        print(f"verify DM to u/{username} failed: {exc}")
```
  - `_render_form(request, values, errors, *, csrf, status_code=200)` returns a `TemplateResponse` (see submit.html changes in Task 8); include `turnstile_site_key=get_settings().turnstile_site_key`.

- [ ] **Step 5: Create `app/templates/submitted.html`**:
```html
{% extends "base.html" %}
{% block title %}Check your Reddit inbox — ufosighting.report{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>Almost there — check your Reddit inbox</h1>
  <p>We sent <strong>u/{{ username }}</strong> a private message with a link to confirm this
     sighting was submitted by you. Click it and your sighting posts to the subreddit right away.</p>
  <p class="muted">Didn't get it, or that's not your account? No problem — a moderator will review
     your sighting and post it. Nothing is lost.</p>
  <a class="btn" href="/">Back to the gallery</a>
</section>
{% endblock %}
```

- [ ] **Step 6: Run — expect pass** (`tests/test_submit.py tests/test_presign.py`).

- [ ] **Step 7: Run full suite** — fix any fallout in `test_public.py` (geocode signature). Commit:
```bash
git add app/routes/submit.py app/routes/public.py app/web.py app/templates/submitted.html tests/
git commit -m "feat: anonymous submission with Turnstile, rate limits, verify DM"
```

---

### Task 7: Verify route

**Files:**
- Create: `app/routes/verify.py`, `app/templates/verify_result.html`
- Modify: `app/main.py` (include router)
- Test: `tests/test_verify_routes.py`

**Interfaces:**
- Consumes: `posting.post_sighting`, `db.get_db`.
- Produces: `GET /verify/{token}` → on a `pending_verify` sighting with matching token: post via bot, render success; else render friendly "no longer valid" (both 200).

- [ ] **Step 1: Failing tests** — `tests/test_verify_routes.py`:
```python
import httpx, respx
from tests.test_public import seed


def _pending(app_db, token="tok-abc"):
    sid = seed(app_db, status="pending_verify", reddit_username="witness1")
    app_db.execute("UPDATE sightings SET verify_token=? WHERE id=?", (token, sid))
    app_db.commit()
    return sid


@respx.mock
def test_valid_token_posts_and_goes_live(client, app_db, monkeypatch):
    monkeypatch.setattr("app.posting.reddit.script_token", lambda: "bot")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_pp"}}}))
    sid = _pending(app_db)
    r = client.get("/verify/tok-abc")
    assert r.status_code == 200 and "live" in r.text.lower()
    row = app_db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["username_verified"] == 1 and row["reddit_post_id"] == "pp"


def test_unknown_token_friendly(client):
    r = client.get("/verify/nope")
    assert r.status_code == 200 and "no longer valid" in r.text.lower()


def test_used_token_friendly(client, app_db, monkeypatch):
    sid = seed(app_db, status="live", reddit_username="w")  # already live, token cleared
    r = client.get("/verify/anything")
    assert r.status_code == 200
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `app/routes/verify.py`**:
```python
from fastapi import APIRouter, Depends, Request
from app import db, posting, reddit
from app.web import templates

router = APIRouter()


@router.get("/verify/{token}")
def verify_click(request: Request, token: str, conn=Depends(db.get_db)):
    row = conn.execute(
        "SELECT id FROM sightings WHERE verify_token=? AND status='pending_verify'", (token,)
    ).fetchone()
    if row is None:
        return templates.TemplateResponse(request, "verify_result.html", {"ok": False})
    try:
        posting.post_sighting(conn, row["id"], verified=True)
    except reddit.RedditError:
        return templates.TemplateResponse(request, "verify_result.html",
                                          {"ok": False, "retry": True})
    slug_row = conn.execute("SELECT id, title FROM sightings WHERE id=?", (row["id"],)).fetchone()
    from app import helpers
    url = f"/sighting/{slug_row['id']}/{helpers.slugify(slug_row['title'])}"
    return templates.TemplateResponse(request, "verify_result.html", {"ok": True, "url": url})
```

- [ ] **Step 4: `app/templates/verify_result.html`**:
```html
{% extends "base.html" %}
{% block title %}Verify — ufosighting.report{% endblock %}
{% block content %}
<section class="panel narrow">
{% if ok %}
  <h1>You're verified — your sighting is live 🛸</h1>
  <p>Thanks for confirming. It's been posted to the subreddit and added to the gallery.</p>
  <a class="btn primary" href="{{ url }}">View your sighting</a>
{% elif retry %}
  <h1>Almost — one hiccup posting to Reddit</h1>
  <p class="muted">We couldn't post it just now. A moderator will finish it shortly, or try your link again in a few minutes.</p>
  <a class="btn" href="/">Back to the gallery</a>
{% else %}
  <h1>This link is no longer valid</h1>
  <p class="muted">It may have already been used or expired. If your sighting wasn't posted, a moderator will review it.</p>
  <a class="btn" href="/">Back to the gallery</a>
{% endif %}
</section>
{% endblock %}
```

- [ ] **Step 5: Register router** in `app/main.py` after public routes:
```python
    from app.routes import verify as verify_routes
    app.include_router(verify_routes.router)
```

- [ ] **Step 6: Run — expect pass. Step 7: Commit**
```bash
git add app/routes/verify.py app/templates/verify_result.html app/main.py tests/test_verify_routes.py
git commit -m "feat: /verify/{token} route — verified click posts via bot"
```

---

### Task 8: Wizard UX — flatpickr, username field, Turnstile widget

**Files:**
- Modify: `app/templates/submit.html`, `static/js/wizard.js`, `static/css/site.css`
- Test: browser pass (post-implementation); no new unit tests (server already covered)

**Interfaces:**
- Consumes: `csrf`, `turnstile_site_key`, `values`, `opts`, `errors`, `show_all` from `_render_form`.

- [ ] **Step 1: Update `submit.html`**
  - Remove the "Posting as u/{{ user.username }}" line (no user).
  - CSRF hidden field: `<input type="hidden" name="csrf_token" value="{{ csrf }}">`.
  - Add a **Reddit username** field on the story step (step 3):
```html
<label>Your Reddit username <span class="muted">— we'll credit you (self-reported until you confirm)</span>
  <input name="reddit_username" required value="{{ values.reddit_username or '' }}"
         placeholder="e.g. your_reddit_name" autocomplete="off">
</label>
```
  - Replace the step-2 date/time `.row` with a single flatpickr field + hidden targets:
```html
<label>When did it happen?
  <input type="text" id="sighted_at_picker" placeholder="Pick a date and time" autocomplete="off">
</label>
<input type="hidden" name="sighted_date" id="sighted_date" value="{{ values.sighted_date or '' }}">
<input type="hidden" name="sighted_time" id="sighted_time" value="{{ values.sighted_time or '' }}">
```
  - Turnstile on the last step (step 7), above the submit button:
```html
{% if turnstile_site_key %}
<div class="cf-turnstile" data-sitekey="{{ turnstile_site_key }}"></div>
{% endif %}
```
  - In `{% block head %}` add flatpickr + turnstile:
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js"></script>
{% if turnstile_site_key %}
<script defer src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
{% endif %}
```

- [ ] **Step 2: flatpickr init in `wizard.js`** (after the tz block):
```javascript
  // flatpickr combined date+time -> hidden sighted_date / sighted_time
  var picker = document.getElementById("sighted_at_picker");
  if (picker && window.flatpickr) {
    var dEl = document.getElementById("sighted_date"), tEl = document.getElementById("sighted_time");
    var initial = dEl.value && tEl.value ? dEl.value + " " + tEl.value : null;
    flatpickr(picker, {
      enableTime: true, dateFormat: "Y-m-d H:i", defaultDate: initial,
      maxDate: "today", time_24hr: true,
      onChange: function (sel, str, fp) {
        if (!sel.length) return;
        var d = sel[0];
        dEl.value = fp.formatDate(d, "Y-m-d");
        tEl.value = fp.formatDate(d, "H:i");
      },
    });
  }
```
  (Remove the old `dur`/date wiring only if it referenced the removed inputs; keep the h/m/s duration block untouched — it uses `dur_h/m/s`.)

- [ ] **Step 3: Make step-2 required-field check work with flatpickr** — the wizard's `requiredOk` checks `input[required]`. Since `sighted_date/time` are hidden (not required attr), add a guard in `wizard.js` `nextBtn` handler: if leaving step 2 with empty `sighted_date`, call `picker.reportValidity?.()` / show the browser prompt. Minimal: mark the picker input `required` and let `reportValidity` fire.

- [ ] **Step 4: CSS** — append flatpickr dark theming to `site.css`:
```css
.flatpickr-calendar { background: var(--panel); color: var(--text); border: 1px solid var(--line); }
.flatpickr-day { color: var(--text); }
.flatpickr-day.selected { background: var(--accent); border-color: var(--accent); color: #05240f; }
.flatpickr-months, .flatpickr-weekdays, span.flatpickr-weekday { color: var(--muted); fill: var(--muted); }
.cf-turnstile { margin: 8px 0; }
```

- [ ] **Step 5: Browser pass** — run the dev server + seed, drive `/submit` anon: flatpickr popup picks a date/time, username field present, autocomplete works, (Turnstile hidden in dev). Confirm submit → "check your inbox" page and a `pending_verify` row. Commit:
```bash
git add app/templates/submit.html static/js/wizard.js static/css/site.css
git commit -m "feat: flatpickr date/time popup, username field, Turnstile widget"
```

---

### Task 9: Mod review queue

**Files:**
- Modify: `app/routes/admin.py`, `app/templates/admin.html`
- Create: `app/templates/review.html`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `require_admin`, `posting.post_sighting`, `auth.csrf_for`.
- Produces: `GET /admin/review` (lists `pending_review`), `POST /admin/review/{id}/approve` (bot posts → live), `POST /admin/review/{id}/reject` (→ rejected). `/admin` shows a pending count + link.

- [ ] **Step 1: Failing tests** — `tests/test_review.py`:
```python
import httpx, respx
from app import auth
from tests.test_public import seed


def _admin(client, app_db):
    sid = auth.create_session(app_db, "tmosh", "tok", 3600)
    client.cookies.set("sid", sid); return sid


def test_review_lists_pending(client, app_db):
    seed(app_db, title="Queued one", status="pending_review", reddit_username="w")
    _admin(client, app_db)
    r = client.get("/admin/review")
    assert r.status_code == 200 and "Queued one" in r.text


def test_review_requires_admin(client):
    assert client.get("/admin/review").status_code == 404


@respx.mock
def test_approve_posts_and_lives(client, app_db, monkeypatch):
    monkeypatch.setattr("app.posting.reddit.script_token", lambda: "bot")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_qq"}}}))
    sid = seed(app_db, status="pending_review", reddit_username="w")
    tok = _admin(client, app_db)
    r = client.post(f"/admin/review/{sid}/approve",
                    data={"csrf_token": auth.csrf_for(tok)}, follow_redirects=False)
    assert r.status_code == 303
    row = app_db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["reddit_post_id"] == "qq"


def test_reject(client, app_db):
    sid = seed(app_db, status="pending_review", reddit_username="w")
    tok = _admin(client, app_db)
    client.post(f"/admin/review/{sid}/reject", data={"csrf_token": auth.csrf_for(tok)},
                follow_redirects=False)
    assert app_db.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()[0] == "rejected"
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Add routes to `app/routes/admin.py`**:
```python
from app import posting

@router.get("/admin/review")
def review_queue(request: Request, conn=Depends(db.get_db), user=Depends(require_admin)):
    rows = conn.execute(
        "SELECT * FROM sightings WHERE status='pending_review' ORDER BY created_at").fetchall()
    media = {}
    for row in rows:
        media[row["id"]] = conn.execute(
            "SELECT r2_key, thumb_key, kind FROM media WHERE sighting_id=? ORDER BY sort_order",
            (row["id"],)).fetchall()
    return templates.TemplateResponse(request, "review.html",
        {"user": user, "rows": rows, "media": media, "csrf_token": auth.csrf_for(user.id)})


@router.post("/admin/review/{sighting_id}/approve")
async def review_approve(request: Request, sighting_id: int,
                         conn=Depends(db.get_db), user=Depends(require_admin)):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf_token","")), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    try:
        posting.post_sighting(conn, sighting_id, verified=False)
    except Exception as exc:
        print(f"approve post failed for {sighting_id}: {exc}")
    return RedirectResponse("/admin/review", status_code=303)


@router.post("/admin/review/{sighting_id}/reject")
async def review_reject(request: Request, sighting_id: int,
                        conn=Depends(db.get_db), user=Depends(require_admin)):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf_token","")), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    conn.execute("UPDATE sightings SET status='rejected' WHERE id=?", (sighting_id,))
    conn.commit()
    return RedirectResponse("/admin/review", status_code=303)
```
(`admin.py` already imports `hmac`, `auth`, `db`, `RedirectResponse`, `HTTPException`, `templates`, `require_admin`, `Request`, `Depends`.)

- [ ] **Step 4: `app/templates/review.html`**:
```html
{% extends "base.html" %}
{% block title %}Review queue — ufosighting.report{% endblock %}
{% block content %}
<h1>Review queue</h1>
<p class="muted">Unverified submissions awaiting approval. Approving posts them to the subreddit as ufosightingsbot.</p>
{% for s in rows %}
<article class="panel" style="margin-bottom:16px">
  <h3>{{ s.title }}</h3>
  <p class="muted">u/{{ s.reddit_username }} · {{ s.sighted_at[:10] }} · {{ s.location_text }}{% if s.country %}, {{ s.country }}{% endif %}{% if s.shape %} · {{ s.shape }}{% endif %}</p>
  <p style="white-space:pre-line">{{ s.description }}</p>
  <div class="thumbstrip">
    {% for m in media[s.id] %}<img src="{{ media_url(m.thumb_key or m.r2_key) }}" alt="">{% endfor %}
  </div>
  <div style="display:flex; gap:10px; margin-top:10px">
    <form method="post" action="/admin/review/{{ s.id }}/approve">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn primary">Approve &amp; post</button>
    </form>
    <form method="post" action="/admin/review/{{ s.id }}/reject">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn danger">Reject</button>
    </form>
  </div>
</article>
{% else %}
<p class="empty">Queue is empty.</p>
{% endfor %}
{% endblock %}
```

- [ ] **Step 5: Link from `admin.html`** — add near the top:
```html
{% set pending = rows_pending_count if rows_pending_count is defined else None %}
<p><a class="btn" href="/admin/review">Review queue</a></p>
```
And in `admin_home` (routes/admin.py) pass a count:
```python
    pending = conn.execute("SELECT COUNT(*) FROM sightings WHERE status='pending_review'").fetchone()[0]
```
add `"pending": pending` to the admin.html context and show `Review queue ({{ pending }})`.

- [ ] **Step 6: Run — expect pass. Step 7: Commit**
```bash
git add app/routes/admin.py app/templates/admin.html app/templates/review.html tests/test_review.py
git commit -m "feat: mod review queue with approve (bot posts) / reject"
```

---

### Task 10: Fallback sweep in the sync timer

**Files:**
- Modify: `sync.py`, `cleanup.py`
- Test: `tests/test_sync.py` (extend), `tests/test_cleanup.py` (extend)

**Interfaces:**
- Consumes: `verify.sweep_pending_verify`.
- Produces: `sync.main()` also runs `verify.sweep_pending_verify(conn, settings.verify_window_hours)`; `cleanup` prunes `rate_events` older than 24h.

- [ ] **Step 1: Failing tests**

`tests/test_sync.py` add:
```python
def test_main_runs_sweep(db_conn, monkeypatch):
    import sync
    called = {}
    monkeypatch.setattr(sync.verify, "sweep_pending_verify", lambda conn, w: called.setdefault("w", w) or 0)
    monkeypatch.setattr(sync, "sync_once", lambda conn: {"checked": 0, "updated": 0})
    monkeypatch.setattr(sync.db, "connect", lambda p: db_conn)
    sync.main()
    assert called["w"] == 6
```

`tests/test_cleanup.py` add:
```python
def test_cleanup_rate_events(db_conn):
    import cleanup
    db_conn.execute("INSERT INTO rate_events (ip, action, created_at) "
                    "VALUES ('1','submit', strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 days'))")
    db_conn.execute("INSERT INTO rate_events (ip, action) VALUES ('1','submit')")
    db_conn.commit()
    assert cleanup.cleanup_rate_events(db_conn) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0] == 1
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement** — in `sync.py` add `from app import verify` and in `main()`:
```python
        s = get_settings()
        result = sync_once(conn)
        swept = verify.sweep_pending_verify(conn, s.verify_window_hours)
        print(f"sync: checked={result['checked']} status_changes={result['updated']} swept={swept}")
```
In `cleanup.py` add:
```python
def cleanup_rate_events(conn, older_than_hours: int = 24) -> int:
    cur = conn.execute(
        "DELETE FROM rate_events WHERE created_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)",
        (f"-{older_than_hours} hours",))
    conn.commit()
    return cur.rowcount
```
and call it in `cleanup.main()` print line.

- [ ] **Step 4: Run — expect pass. Step 5: Commit**
```bash
git add sync.py cleanup.py tests/test_sync.py tests/test_cleanup.py
git commit -m "feat: verify-window sweep in sync, rate_events pruning in cleanup"
```

---

### Task 11: Ingest poller

**Files:**
- Create: `ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `reddit.list_flair_posts/script_token`, `r2.put_bytes/public_url`, `db.connect`.
- Produces: `ingest_once(conn, *, limit=100, after=None) -> dict` ({"seen","added"}); `ingest_post(conn, post: dict) -> bool` (create `source='reddit'` sighting if new; dedup on `reddit_post_id`; download image/gallery media best-effort); `download_media(post) -> list[tuple[bytes, str, str]]` (bytes, content_type, ext) for i.redd.it + gallery images; `main(backfill: bool)`.

- [ ] **Step 1: Failing tests** — `tests/test_ingest.py`:
```python
import ingest


def _post(pid="p1", **over):
    d = {"id": pid, "title": "Orb over town", "author": "witness9",
         "selftext": "Saw an orb.", "created_utc": 1751000000,
         "permalink": f"/r/UFOs/comments/{pid}/x/", "url": "https://reddit.com/x",
         "link_flair_text": "Sighting", "is_self": True}
    d.update(over); return d


def test_ingest_creates_reddit_source_entry(db_conn, monkeypatch):
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    assert ingest.ingest_post(db_conn, _post()) is True
    row = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='p1'").fetchone()
    assert row["source"] == "reddit" and row["status"] == "live"
    assert row["reddit_username"] == "witness9"


def test_ingest_dedup(db_conn, monkeypatch):
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    ingest.ingest_post(db_conn, _post())
    assert ingest.ingest_post(db_conn, _post()) is False  # already present
    assert db_conn.execute("SELECT COUNT(*) FROM sightings WHERE reddit_post_id='p1'").fetchone()[0] == 1


def test_ingest_once_uses_listing(db_conn, monkeypatch):
    monkeypatch.setattr(ingest.reddit, "script_token", lambda: "t")
    monkeypatch.setattr(ingest.reddit, "list_flair_posts",
                        lambda tok, **k: ([_post("a"), _post("b")], None))
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    res = ingest.ingest_once(db_conn)
    assert res == {"seen": 2, "added": 2}


def test_ingest_media_failure_non_fatal(db_conn, monkeypatch):
    def boom(post): raise RuntimeError("net")
    monkeypatch.setattr(ingest, "download_media", boom)
    assert ingest.ingest_post(db_conn, _post()) is True  # entry still created
    assert db_conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `ingest.py`**:
```python
"""Ingest Sighting-flaired posts from the subreddit into the gallery.
Run by ufosighting-ingest.timer; `--backfill` walks history once."""
import sys
from datetime import datetime, timezone

import httpx

from app import db, r2, reddit
from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"


def download_media(post: dict) -> list[tuple[bytes, str, str]]:
    out = []
    url = post.get("url", "")
    gallery = post.get("media_metadata")
    if gallery:
        for item in gallery.values():
            if item.get("e") == "Image" and item.get("s", {}).get("u"):
                src = item["s"]["u"].replace("&amp;", "&")
                out.append(_fetch_image(src))
    elif any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        out.append(_fetch_image(url))
    elif "i.redd.it" in url:
        out.append(_fetch_image(url))
    return [m for m in out if m]


def _fetch_image(url: str):
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"User-Agent": get_settings().user_agent})
    if resp.status_code != 200:
        return None
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/gif": ".gif"}.get(ct, ".jpg")
    return resp.content, ct, ext


def ingest_post(conn, post: dict) -> bool:
    pid = post["id"]
    if conn.execute("SELECT 1 FROM sightings WHERE reddit_post_id=?", (pid,)).fetchone():
        return False
    sighted_at = datetime.fromtimestamp(post.get("created_utc", 0), timezone.utc).strftime(ISO)
    title = (post.get("title") or "Untitled sighting")[:300]
    cur = conn.execute(
        """INSERT INTO sightings (source, reddit_username, title, description, sighted_at,
             tz_name, location_text, reddit_post_id, status)
           VALUES ('reddit', ?, ?, ?, ?, 'UTC', '', ?, 'live')""",
        (post.get("author") or "unknown", title, post.get("selftext") or "", sighted_at, pid),
    )
    sid = cur.lastrowid
    conn.commit()
    try:
        for i, (data, ct, ext) in enumerate(download_media(post)):
            import uuid
            now = datetime.now(timezone.utc)
            key = f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"
            r2.put_bytes(key, data, ct)
            conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) "
                         "VALUES (?,?, 'image', ?)", (sid, key, i))
        conn.commit()
    except Exception as exc:
        print(f"ingest media for {pid} failed: {exc}")
    return True


def ingest_once(conn, *, limit=100, after=None) -> dict:
    s = get_settings()
    token = reddit.script_token()
    posts, _after = reddit.list_flair_posts(token, subreddit=s.subreddit,
                                            flair="Sighting", limit=limit, after=after)
    added = sum(1 for p in posts if ingest_post(conn, p))
    return {"seen": len(posts), "added": added}


def main(backfill: bool = False) -> None:
    conn = db.connect(get_settings().db_path)
    try:
        if backfill:
            after, total = None, 0
            while True:
                s = get_settings()
                token = reddit.script_token()
                posts, after = reddit.list_flair_posts(token, subreddit=s.subreddit,
                                                       flair="Sighting", limit=100, after=after)
                if not posts:
                    break
                total += sum(1 for p in posts if ingest_post(conn, p))
                if not after:
                    break
            print(f"ingest backfill: added={total}")
        else:
            print("ingest:", ingest_once(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main(backfill="--backfill" in sys.argv)
```

- [ ] **Step 4: Run — expect pass. Step 5: Commit**
```bash
git add ingest.py tests/test_ingest.py
git commit -m "feat: ingest Sighting-flaired posts (dedup, image/gallery media, backfill)"
```

---

### Task 12: Deploy artifacts + full-suite gate + browser pass

**Files:**
- Create: `deploy/ufosighting-ingest.service`, `deploy/ufosighting-ingest.timer`
- Modify: `deploy/RUNBOOK.md`, `.env.example` (verify complete)
- Test: full suite green + Playwright pass

- [ ] **Step 1: Ingest systemd units**

`deploy/ufosighting-ingest.service`:
```ini
[Unit]
Description=ufosighting.report Sighting-post ingest

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/ufosighting
EnvironmentFile=/home/ubuntu/ufosighting/.env
ExecStart=/home/ubuntu/ufosighting/.venv/bin/python ingest.py
```
`deploy/ufosighting-ingest.timer`:
```ini
[Unit]
Description=Run Sighting ingest every 10 minutes

[Timer]
OnCalendar=*:0/10
RandomizedDelaySec=45
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 2: RUNBOOK** — add a "Pivot: anon + verify + ingest" section: make `ufosightingsbot` a mod/approved-submitter with `submit`+`privatemessages`+`read`; create Turnstile widget → keys in `.env`; `SCRIPT_*` = bot creds; install `ufosighting-ingest.timer`; run `python ingest.py --backfill` once.

- [ ] **Step 3: Full suite** — `.venv/bin/pytest -q` → all green. Fix any stragglers.

- [ ] **Step 4: Browser pass** — dev server + seed; anon `/submit` end to end (flatpickr, autocomplete, submit → inbox page → `pending_verify` row); `/admin/review` approve path with mocked Reddit; `/verify/<token>` success page.

- [ ] **Step 5: Commit**
```bash
git add deploy/ .env.example
git commit -m "feat: ingest timer, runbook + env for anon/verify/ingest pivot"
```

---

## Post-plan notes

- **Deploy is a separate step** (not in this plan's tasks): rsync to the VM, run the idempotent migration (automatic on service restart via `init_db`), set `SCRIPT_*` + Turnstile keys in the VM `.env`, install the ingest timer, make `ufosightingsbot` a mod of r/tmoshtest, backfill, then end-to-end test.
- **Video ingest** (v.redd.it audio mux) is intentionally deferred — image + gallery covers the common sighting case; video posts still ingest as entries (no local media) and link to Reddit.
- **OAuth path** remains mounted and dormant; when the web app is approved it becomes the "verified without DM" fast-lane.
