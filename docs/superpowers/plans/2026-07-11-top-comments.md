# Top 10 Reddit Comments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each sighting stores and displays its top 10 upvoted Reddit comments, refreshed by the existing sync tiers.

**Architecture:** `comments` table (wholesale replace per refresh); `app/comments.py` fetches top-level comments via `GET /comments/{id}?sort=top&depth=1`; `sync.py` piggybacks refreshes for live rows; `backfill_comments.py` one-shot seeds existing rows; detail page renders via the `reddit_md` filter. Spec: `docs/superpowers/specs/2026-07-11-top-comments-design.md`.

**Tech Stack:** Python 3.12, httpx + respx, sqlite3, Jinja2, pytest.

## Global Constraints

- Top 10 by score; top-level comments only; skip AutoModerator, the bot (`settings.script_username`), and deleted/empty bodies.
- Fetch failure is best-effort: return `[]`, never raise out of sync; existing stored comments are kept on failure and for non-live posts (archive philosophy).
- Sync throttle: 1s sleep between comment fetches; backfill 2s.
- Comments render with `reddit_md`; NOT indexed in Meilisearch.
- `pytest -q` green before each commit.

---

### Task 1: `comments` table

**Files:** Modify `app/db.py`; test `tests/test_db.py`.
**Interfaces:** table `comments(reddit_comment_id PK, sighting_id FK CASCADE, author, body, score, created_utc, permalink, fetched_at)` + index `(sighting_id, score DESC)`.

- [ ] Step 1: failing test in `tests/test_db.py`:

```python
def test_comments_table(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, score)"
                    " VALUES ('c1', ?, 'alice', '**wow**', 42)", (sid,))
    row = db_conn.execute("SELECT * FROM comments").fetchone()
    assert row["score"] == 42 and row["permalink"] == ""
    # cascade with the sighting
    db_conn.execute("DELETE FROM sightings WHERE id=?", (sid,))
    assert db_conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0
```

- [ ] Step 2: run → no such table.
- [ ] Step 3: add DDL from the spec to `SCHEMA_TABLES` (before FTS) and the index to `SCHEMA_INDEXES`.
- [ ] Step 4: `pytest -q` green.
- [ ] Step 5: commit `feat: comments table`.

### Task 2: `app/comments.py`

**Files:** create `app/comments.py`; test `tests/test_comments.py`.
**Interfaces:** `TOP_N = 10`; `fetch_top_comments(token, post_id, limit=50) -> list[dict]`; `refresh_for_sighting(conn, token, sighting_id, reddit_post_id) -> int`.

- [ ] Step 1: failing tests:

```python
import httpx
import respx

from app import comments
from tests.test_db import _insert_sighting


def _listing(children):
    return httpx.Response(200, json=[
        {"data": {"children": []}},               # [0] = the post
        {"data": {"children": children}},          # [1] = comments
    ])


def _c(cid, author, body, score, **over):
    d = {"id": cid, "author": author, "body": body, "score": score,
         "created_utc": 1751000000, "permalink": f"/r/UFOs/comments/p1/x/{cid}/"}
    d.update(over)
    return {"kind": "t1", "data": d}


@respx.mock
def test_fetch_skips_bot_automod_deleted_and_caps_at_ten():
    kids = [_c(f"c{i}", f"user{i}", f"body {i}", 100 - i) for i in range(12)]
    kids.insert(0, _c("cb", "modbot", "details comment", 999))          # the bot (conftest SCRIPT_USERNAME)
    kids.insert(0, _c("ca", "AutoModerator", "sticky", 998))
    kids.insert(0, _c("cd", "ghost", "[deleted]", 997))
    kids.append({"kind": "more", "data": {"children": ["x"]}})
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=_listing(kids))
    out = comments.fetch_top_comments("tok", "p1")
    assert len(out) == 12  # filtering happens at fetch; capping in refresh
    assert all(c["author"] not in ("AutoModerator", "modbot") for c in out)
    assert all(c["body"] not in ("[deleted]", "[removed]") for c in out)


@respx.mock
def test_fetch_http_error_returns_empty():
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=httpx.Response(500))
    assert comments.fetch_top_comments("tok", "p1") == []


@respx.mock
def test_refresh_replaces_and_caps(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, score)"
                    " VALUES ('stale', ?, 'old', 'gone from reddit', 1)", (sid,))
    kids = [_c(f"c{i}", f"user{i}", f"body {i}", i) for i in range(12)]  # scores 0..11
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=_listing(kids))
    n = comments.refresh_for_sighting(db_conn, "tok", sid, "p1")
    assert n == 10
    rows = db_conn.execute("SELECT * FROM comments WHERE sighting_id=? ORDER BY score DESC",
                           (sid,)).fetchall()
    assert len(rows) == 10 and rows[0]["score"] == 11 and rows[-1]["score"] == 2
    assert not any(r["reddit_comment_id"] == "stale" for r in rows)


@respx.mock
def test_refresh_keeps_existing_on_fetch_failure(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, score)"
                    " VALUES ('keep', ?, 'a', 'b', 1)", (sid,))
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=httpx.Response(500))
    assert comments.refresh_for_sighting(db_conn, "tok", sid, "p1") == 0
    assert db_conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
```

- [ ] Step 2: run → import error.
- [ ] Step 3: implement:

```python
"""Top Reddit comments per sighting: fetch + wholesale-replace storage.
Best-effort everywhere — a failed fetch never raises out of sync and never
clobbers previously stored comments (archive philosophy)."""
import httpx

from app.config import get_settings

TOP_N = 10
SKIP_AUTHORS = {"AutoModerator"}
SKIP_BODIES = {"", "[deleted]", "[removed]"}


def fetch_top_comments(token: str, post_id: str, *, limit: int = 50) -> list[dict]:
    s = get_settings()
    try:
        resp = httpx.get(
            f"https://oauth.reddit.com/comments/{post_id}",
            params={"sort": "top", "depth": 1, "limit": limit},
            headers={"Authorization": f"bearer {token}", "User-Agent": s.user_agent},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        listing = resp.json()
        if len(listing) < 2:
            return []
        out = []
        skip = SKIP_AUTHORS | {s.script_username}
        for child in listing[1]["data"]["children"]:
            if child.get("kind") != "t1":
                continue
            d = child.get("data", {})
            if d.get("author") in skip or (d.get("body") or "").strip() in SKIP_BODIES:
                continue
            out.append({"id": d.get("id"), "author": d.get("author"),
                        "body": d.get("body"), "score": int(d.get("score") or 0),
                        "created_utc": int(d.get("created_utc") or 0),
                        "permalink": d.get("permalink") or ""})
        return out
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []


def refresh_for_sighting(conn, token: str, sighting_id: int, reddit_post_id: str) -> int:
    fetched = fetch_top_comments(token, reddit_post_id)
    if not fetched:
        return 0  # keep whatever we had — fetch failures must not erase the archive
    top = sorted(fetched, key=lambda c: c["score"], reverse=True)[:TOP_N]
    conn.execute("DELETE FROM comments WHERE sighting_id=?", (sighting_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO comments (reddit_comment_id, sighting_id, author, body,"
        " score, created_utc, permalink) VALUES (?,?,?,?,?,?,?)",
        [(c["id"], sighting_id, c["author"], c["body"], c["score"],
          c["created_utc"], c["permalink"]) for c in top])
    conn.commit()
    return len(top)
```

- [ ] Step 4: `pytest -q` green.
- [ ] Step 5: commit `feat: top-comments fetch + storage`.

### Task 3: sync piggyback

**Files:** modify `sync.py`; test `tests/test_sync.py`.
**Interfaces:** `sync_once(conn, *, window_hours=72, comment_sleep=time.sleep)` gains a comment-refresh pass; result dict gains `"comments"` (count of refreshed posts).

- [ ] Step 1: failing test:

```python
def test_sync_refreshes_comments_for_live_only(db_conn, monkeypatch):
    live = _seed(db_conn, "aaa", "live")
    _seed(db_conn, "bbb", "live")  # becomes removed during this sync
    _fake_infos(monkeypatch, {"aaa": reddit.PostInfo(None, 5, 2),
                              "bbb": reddit.PostInfo("moderator", 1, 0)})
    monkeypatch.setattr(sync.reddit, "script_token", lambda: "tok")
    refreshed = []
    monkeypatch.setattr(sync.comments, "refresh_for_sighting",
                        lambda conn, tok, sid, pid: refreshed.append(pid) or 3)
    result = sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert refreshed == ["aaa"]
    assert result["comments"] == 1
```

- [ ] Step 2: run → fail.
- [ ] Step 3: implement in `sync.py` — import `time` and `comments`; in `sync_once`, track `(id, post_id, new_status)` per row; after the score/status commit:

```python
    token = reddit.script_token()
    refreshed = 0
    for sid, pid, new_status in live_rows:      # rows whose new status is 'live'
        if comments.refresh_for_sighting(conn, token, sid, pid):
            refreshed += 1
        comment_sleep(1)
    return {"checked": len(rows), "updated": updated, "comments": refreshed}
```

(`comment_sleep=time.sleep` keyword param; existing `main()` prints the new key too.)

- [ ] Step 4: `pytest -q` green (update the existing sync tests for the new result key and the `script_token` call).
- [ ] Step 5: commit `feat: sync refreshes top comments for live posts`.

### Task 4: detail page section

**Files:** modify `app/routes/public.py` (detail route), `app/templates/detail.html`, `static/css/site.css`; test `tests/test_public.py`.

- [ ] Step 1: failing test:

```python
def test_detail_shows_top_comments(client_with_data):  # reuse this file's fixture style
    # insert a live sighting + 2 comments, GET its detail page
    ...
    assert "Top comments on Reddit" in html
    assert "<strong>wow</strong>" in html          # reddit_md rendered
    assert "u/alice" in html
```

(Adapt to the file's existing fixtures — it has helpers that create sightings via the `client` fixture's DB; follow the pattern of the nearest existing detail-page test.)

- [ ] Step 2: run → fail.
- [ ] Step 3: route — in `detail()`, after loading the sighting row:

```python
    comment_rows = conn.execute(
        "SELECT author, body, score, permalink, created_utc FROM comments "
        "WHERE sighting_id=? ORDER BY score DESC", (sighting_id,)).fetchall()
```

pass `comments=comment_rows` into the template context. Template, after the description block:

```html
{% if comments %}
<section class="top-comments">
  <h2>Top comments on Reddit</h2>
  {% for c in comments %}
  <div class="comment">
    <div class="comment-meta">
      <a href="https://www.reddit.com/user/{{ c.author }}">u/{{ c.author }}</a>
      <span class="score">▲ {{ c.score }}</span>
      {% if c.permalink %}<a class="muted" href="https://www.reddit.com{{ c.permalink }}">permalink</a>{% endif %}
    </div>
    <div class="md-body">{{ c.body|reddit_md|safe }}</div>
  </div>
  {% endfor %}
</section>
{% endif %}
```

CSS: `.top-comments .comment { border-top: 1px solid #232c47; padding: 10px 0; } .comment-meta { display:flex; gap:10px; font-size:0.85rem; } .comment-meta .score { color: var(--accent); }`

- [ ] Step 4: `pytest -q` green.
- [ ] Step 5: commit `feat: top-comments section on detail pages`.

### Task 5: `backfill_comments.py`

**Files:** create `backfill_comments.py` (no unit test — 20-line orchestration of tested parts; verified live).

```python
"""One-shot: seed top comments for every public sighting with a reddit post.
Safe to re-run. 2s throttle — the script app is shared with ufosarchive."""
import time

from app import comments, db, reddit, search
from app.config import get_settings

if __name__ == "__main__":
    conn = db.connect(get_settings().db_path)
    try:
        token = reddit.script_token()
        rows = conn.execute(
            f"""SELECT id, reddit_post_id FROM sightings
                WHERE reddit_post_id IS NOT NULL
                  AND status IN ('live','deleted_by_user','removed_on_reddit')
                ORDER BY id""").fetchall()
        total = 0
        for r in rows:
            n = comments.refresh_for_sighting(conn, token, r["id"], r["reddit_post_id"])
            total += 1 if n else 0
            time.sleep(2)
        print(f"backfill_comments: posts={len(rows)} with_comments={total}")
    finally:
        conn.close()
```

- [ ] `python3 -m py_compile backfill_comments.py`; commit `feat: comments backfill script`.

### Task 6: Deploy + backfill (ops — WAITING on ufosightingsbot token recovery)

- [ ] User approves deploy → `bash deploy/deploy.sh`.
- [ ] Run `backfill_comments.py` on Oracle once the token grant works again.
- [ ] Spot-check a busy sighting's detail page via VM localhost.
