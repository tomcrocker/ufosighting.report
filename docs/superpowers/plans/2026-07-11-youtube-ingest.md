# YouTube Media Ingest (yt-dlp) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sighting posts whose video lives on YouTube (link posts AND body links) get their video downloaded (on the local VM — YouTube blocks datacenter IPs), stored in R2, and attached as gallery media; existing media-less rows are repaired retroactively.

**Architecture:** Oracle detects + queues (`yt_jobs` table); a systemd-timer worker on local VM 192.168.8.224 claims jobs over SSH, runs yt-dlp, uploads to R2, reports back via a `ytq.py` CLI that inserts the media row and refreshes Meilisearch. Spec: `docs/superpowers/specs/2026-07-11-youtube-ingest-design.md`.

**Tech Stack:** Python 3.12, sqlite3, yt-dlp (local VM), boto3, ssh, systemd timer, pytest.

## Global Constraints

- First YouTube URL only per post; `post.url` beats `selftext`.
- Video IDs are `[A-Za-z0-9_-]{11}`; channel/user/playlist URLs never match.
- Download: ≤720p mp4, `--max-filesize 200M`, `--no-playlist`, 600s timeout.
- Max 3 attempts per job, then `status='failed'` with `last_error` (≤300 chars).
- Reddit API throttle: 2s sleep per re-fetch in the retroactive scan.
- All Oracle-side code unit-tested without network; run `pytest -q` green before each commit.

---

### Task 1: URL detection — `app/ytdetect.py`

**Files:**
- Create: `app/ytdetect.py`
- Test: `tests/test_ytdetect.py`

**Interfaces:**
- Produces: `find_in_text(text: str | None) -> str | None` and `find_youtube_url(post: dict) -> str | None`, both returning a canonical `https://www.youtube.com/watch?v=<id>` or `None`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from app import ytdetect

CANON = "https://www.youtube.com/watch?v=XHWPQEJ_TVA"


@pytest.mark.parametrize("text", [
    "https://www.youtube.com/watch?v=XHWPQEJ_TVA",
    "https://youtube.com/shorts/XHWPQEJ_TVA",
    "https://youtu.be/XHWPQEJ_TVA",
    "https://m.youtube.com/watch?v=XHWPQEJ_TVA&feature=share",
    "https://www.youtube.com/watch?app=desktop&v=XHWPQEJ_TVA",
    "https://music.youtube.com/watch?v=XHWPQEJ_TVA",
    "https://www.youtube.com/live/XHWPQEJ_TVA",
    "https://www.youtube.com/embed/XHWPQEJ_TVA",
    "Saw this last night: https://youtu.be/XHWPQEJ_TVA amazing footage",
])
def test_find_in_text_variants(text):
    assert ytdetect.find_in_text(text) == CANON


def test_first_match_wins():
    text = ("video https://youtu.be/XHWPQEJ_TVA and my channel promo "
            "https://youtu.be/aaaaaaaaaaa")
    assert ytdetect.find_in_text(text) == CANON


@pytest.mark.parametrize("text", [
    None, "", "no links here",
    "https://www.youtube.com/@somechannel",
    "https://www.youtube.com/channel/UCabcdefghij",
    "https://www.youtube.com/playlist?list=PLxyz",
    "https://v.redd.it/abc123",
])
def test_find_in_text_non_matches(text):
    assert ytdetect.find_in_text(text) is None


def test_post_url_beats_selftext():
    post = {"url": "https://youtu.be/XHWPQEJ_TVA",
            "selftext": "also https://youtu.be/aaaaaaaaaaa"}
    assert ytdetect.find_youtube_url(post) == CANON


def test_post_body_link_only():
    post = {"url": "https://www.reddit.com/r/UFOs/comments/1upvfrb/x/",
            "selftext": "Footage here https://youtu.be/XHWPQEJ_TVA"}
    assert ytdetect.find_youtube_url(post) == CANON


def test_post_no_youtube():
    assert ytdetect.find_youtube_url({"url": "https://i.redd.it/a.jpg",
                                      "selftext": ""}) is None
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_ytdetect.py -q` → import error.

- [ ] **Step 3: Implement `app/ytdetect.py`**

```python
"""Detect YouTube URLs in Reddit posts — link posts and selftext bodies.

Only the first URL counts: sighting posts lead with their video; later
links are typically channel promo. Channel/user/playlist URLs never match
because the pattern requires a video-path form (watch/shorts/live/embed/v)
or youtu.be."""
import re

_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube(?:-nocookie)?\.com/"
    r"(?:watch\?(?:[^\s()\[\]]*&)?v=|shorts/|live/|embed/|v/)"
    r"|youtu\.be/)"
    rf"({_VIDEO_ID})"
)


def find_in_text(text: str | None) -> str | None:
    """First YouTube video URL in free text, canonicalised, or None."""
    if not text:
        return None
    m = _PATTERN.search(text)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def find_youtube_url(post: dict) -> str | None:
    """YouTube URL for a Reddit post: link-post URL first, then selftext."""
    return find_in_text(post.get("url")) or find_in_text(post.get("selftext"))
```

- [ ] **Step 4: `pytest tests/test_ytdetect.py -q` → all pass; `pytest -q` green.**
- [ ] **Step 5: Commit** — `git add app/ytdetect.py tests/test_ytdetect.py && git commit -m "feat: YouTube URL detection for link posts and selftext"`

### Task 2: `yt_jobs` table

**Files:**
- Modify: `app/db.py` (SCHEMA_TABLES + SCHEMA_INDEXES)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: table `yt_jobs(id, sighting_id UNIQUE→sightings CASCADE, url, status pending|done|failed, attempts, last_error, created_at, updated_at)`.

- [ ] **Step 1: Failing test** (append to `tests/test_db.py`; use the suite's existing in-memory conn fixture pattern)

```python
def test_yt_jobs_table(conn):
    conn.execute("INSERT INTO sightings (reddit_username, title, sighted_at) "
                 "VALUES ('u','t','2026-01-01T00:00:00Z')")
    sid = conn.execute("SELECT id FROM sightings").fetchone()["id"]
    conn.execute("INSERT INTO yt_jobs (sighting_id, url) VALUES (?, 'https://www.youtube.com/watch?v=XHWPQEJ_TVA')", (sid,))
    row = conn.execute("SELECT * FROM yt_jobs").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 0
    # UNIQUE(sighting_id): second enqueue is ignored
    conn.execute("INSERT OR IGNORE INTO yt_jobs (sighting_id, url) VALUES (?, 'x')", (sid,))
    assert conn.execute("SELECT COUNT(*) FROM yt_jobs").fetchone()[0] == 1
```

- [ ] **Step 2: Run → fails (no such table).**
- [ ] **Step 3: Add to `SCHEMA_TABLES` in `app/db.py`** (before the FTS virtual table)

```sql
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
```

and to `SCHEMA_INDEXES`: `CREATE INDEX IF NOT EXISTS idx_yt_jobs_status ON yt_jobs(status);`

- [ ] **Step 4: `pytest -q` green.**
- [ ] **Step 5: Commit** — `git commit -m "feat: yt_jobs queue table"`

### Task 3: Ingest enqueue hook

**Files:**
- Modify: `ingest.py` (`ingest_post`, media insert block)
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `ytdetect.find_youtube_url(post)`.
- Produces: pending `yt_jobs` row when a post yields no reddit-hosted media but contains a YouTube URL; committed atomically with the sighting row.

- [ ] **Step 1: Failing tests** (follow test_ingest.py's existing fixtures/monkeypatch style for download_media/extract/geocode)

```python
def test_ingest_enqueues_youtube_body_link(conn, monkeypatch):
    # post: self post, no reddit media, youtube in body
    post = make_post(id="ytbody1", selftext="footage https://youtu.be/XHWPQEJ_TVA")
    ingest.ingest_post(conn, post)
    job = conn.execute("SELECT * FROM yt_jobs").fetchone()
    assert job["url"] == "https://www.youtube.com/watch?v=XHWPQEJ_TVA"
    assert job["status"] == "pending"


def test_ingest_no_yt_job_when_reddit_media(conn, monkeypatch):
    # download_media returns a fake video → no yt_jobs row even with a link
    ...
    assert conn.execute("SELECT COUNT(*) FROM yt_jobs").fetchone()[0] == 0
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — in `ingest_post`, import `ytdetect`; after the media upload loop, before `conn.commit()`:

```python
    if not media_items:
        yt_url = ytdetect.find_youtube_url(post)
        if yt_url:
            conn.execute("INSERT OR IGNORE INTO yt_jobs (sighting_id, url) VALUES (?,?)",
                         (sid, yt_url))
```

- [ ] **Step 4: `pytest -q` green.**
- [ ] **Step 5: Commit** — `git commit -m "feat: ingest queues YouTube downloads for media-less posts"`

### Task 4: Queue CLI — `ytq.py`

**Files:**
- Create: `ytq.py` (repo root, alongside sync.py/ingest.py)
- Test: `tests/test_ytq.py`

**Interfaces:**
- Produces: `claim(conn, limit=5) -> list[dict]` (keys job_id/sighting_id/url), `done(conn, job_id, key, size)`, `fail(conn, job_id, error)`; CLI `claim|done|fail` printing JSON / "ok".
- Consumes: `search.index_sightings(conn, [sid])` (no-op when MEILI_URL empty, as in tests).

- [ ] **Step 1: Failing tests**

```python
import ytq


def _mk_job(conn, url="https://www.youtube.com/watch?v=XHWPQEJ_TVA"):
    conn.execute("INSERT INTO sightings (reddit_username, title, sighted_at, status) "
                 "VALUES ('u','t','2026-01-01T00:00:00Z','live')")
    sid = conn.execute("SELECT MAX(id) FROM sightings").fetchone()[0]
    conn.execute("INSERT INTO yt_jobs (sighting_id, url) VALUES (?,?)", (sid, url))
    return sid, conn.execute("SELECT MAX(id) FROM yt_jobs").fetchone()[0]


def test_claim_lists_pending_only(conn):
    sid, jid = _mk_job(conn)
    jobs = ytq.claim(conn)
    assert jobs == [{"job_id": jid, "sighting_id": sid, "url":
                     "https://www.youtube.com/watch?v=XHWPQEJ_TVA"}]
    ytq.done(conn, jid, "uploads/2026/07/yt_x.mp4", 123)
    assert ytq.claim(conn) == []


def test_done_inserts_media_and_marks(conn):
    sid, jid = _mk_job(conn)
    ytq.done(conn, jid, "uploads/2026/07/yt_x.mp4", 4567)
    m = conn.execute("SELECT * FROM media").fetchone()
    assert (m["sighting_id"], m["kind"], m["r2_key"], m["size_bytes"]) == \
        (sid, "video", "uploads/2026/07/yt_x.mp4", 4567)
    assert conn.execute("SELECT status FROM yt_jobs WHERE id=?", (jid,)).fetchone()[0] == "done"


def test_fail_retries_then_fails(conn):
    sid, jid = _mk_job(conn)
    ytq.fail(conn, jid, "boom")
    row = conn.execute("SELECT * FROM yt_jobs WHERE id=?", (jid,)).fetchone()
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "boom")
    ytq.fail(conn, jid, "boom2")
    ytq.fail(conn, jid, "boom3")
    assert conn.execute("SELECT status, attempts FROM yt_jobs WHERE id=?",
                        (jid,)).fetchone()[:] == ("failed", 3)
    assert ytq.claim(conn) == []
```

- [ ] **Step 2: Run → import error.**
- [ ] **Step 3: Implement `ytq.py`**

```python
"""Queue CLI for the YouTube download worker on the local VM.

The worker SSHes in and runs (cwd matters — .env loads relative to cwd):
    cd /home/ubuntu/ufosighting && .venv/bin/python ytq.py claim
    ... ytq.py done <job_id> --key uploads/2026/07/yt_abc.mp4 --size 12345
    ... ytq.py fail <job_id> --error "yt-dlp: video unavailable"
"""
import argparse
import json

from app import db, search
from app.config import get_settings

MAX_ATTEMPTS = 3
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def claim(conn, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT id, sighting_id, url FROM yt_jobs "
        "WHERE status='pending' AND attempts < ? ORDER BY id LIMIT ?",
        (MAX_ATTEMPTS, limit)).fetchall()
    return [{"job_id": r["id"], "sighting_id": r["sighting_id"], "url": r["url"]}
            for r in rows]


def done(conn, job_id: int, key: str, size: int) -> None:
    job = conn.execute("SELECT sighting_id FROM yt_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise SystemExit(f"no such job {job_id}")
    sid = job["sighting_id"]
    order = conn.execute("SELECT COALESCE(MAX(sort_order)+1, 0) FROM media "
                         "WHERE sighting_id=?", (sid,)).fetchone()[0]
    conn.execute("INSERT INTO media (sighting_id, r2_key, kind, size_bytes, sort_order) "
                 "VALUES (?,?,'video',?,?)", (sid, key, size, order))
    conn.execute(f"UPDATE yt_jobs SET status='done', updated_at={_NOW} WHERE id=?",
                 (job_id,))
    conn.commit()
    search.index_sightings(conn, [sid])


def fail(conn, job_id: int, error: str) -> None:
    row = conn.execute("SELECT attempts FROM yt_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no such job {job_id}")
    attempts = row["attempts"] + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    conn.execute(f"UPDATE yt_jobs SET attempts=?, status=?, last_error=?, "
                 f"updated_at={_NOW} WHERE id=?",
                 (attempts, status, (error or "")[:300], job_id))
    conn.commit()


def main() -> None:
    p = argparse.ArgumentParser(description="YouTube job queue")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--limit", type=int, default=5)
    d = sub.add_parser("done")
    d.add_argument("job_id", type=int)
    d.add_argument("--key", required=True)
    d.add_argument("--size", type=int, required=True)
    f = sub.add_parser("fail")
    f.add_argument("job_id", type=int)
    f.add_argument("--error", default="")
    args = p.parse_args()
    conn = db.connect(get_settings().db_path)
    try:
        if args.cmd == "claim":
            print(json.dumps(claim(conn, args.limit)))
        elif args.cmd == "done":
            done(conn, args.job_id, args.key, args.size)
            print("ok")
        else:
            fail(conn, args.job_id, args.error)
            print("ok")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: `pytest -q` green.**
- [ ] **Step 5: Commit** — `git commit -m "feat: ytq queue CLI for the local download worker"`

### Task 5: Retroactive scan — `scan_youtube.py`

**Files:**
- Create: `scan_youtube.py`
- Test: `tests/test_scan_youtube.py`

**Interfaces:**
- Produces: `scan(conn, *, fetch=None, sleep=time.sleep) -> dict` with keys scanned/body_hits/api_hits/enqueued; `fetch(post_id)->dict|None` injectable for tests (None ⇒ real `reddit.fetch_post`).

- [ ] **Step 1: Failing tests**

```python
import scan_youtube


def _mk_sighting(conn, desc="", pid=None, with_media=False):
    conn.execute("INSERT INTO sightings (source, reddit_username, title, description, "
                 "sighted_at, reddit_post_id, status) VALUES "
                 "('reddit','u','t',?,'2026-01-01T00:00:00Z',?,'live')", (desc, pid))
    sid = conn.execute("SELECT MAX(id) FROM sightings").fetchone()[0]
    if with_media:
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind) "
                     "VALUES (?,'uploads/x.mp4','video')", (sid,))
    return sid


def test_body_hit_no_api_call(conn):
    sid = _mk_sighting(conn, desc="see https://youtu.be/XHWPQEJ_TVA", pid="aaa")
    calls = []
    stats = scan_youtube.scan(conn, fetch=lambda p: calls.append(p),
                              sleep=lambda s: None)
    assert stats == {"scanned": 1, "body_hits": 1, "api_hits": 0, "enqueued": 1}
    assert calls == []
    assert conn.execute("SELECT sighting_id FROM yt_jobs").fetchone()[0] == sid


def test_api_fallback_for_link_post(conn):
    _mk_sighting(conn, desc="", pid="bbb")
    stats = scan_youtube.scan(
        conn, fetch=lambda p: {"url": "https://youtu.be/XHWPQEJ_TVA", "selftext": ""},
        sleep=lambda s: None)
    assert stats["api_hits"] == 1 and stats["enqueued"] == 1


def test_skips_rows_with_media_or_jobs(conn):
    _mk_sighting(conn, desc="https://youtu.be/XHWPQEJ_TVA", pid="ccc", with_media=True)
    sid = _mk_sighting(conn, desc="https://youtu.be/XHWPQEJ_TVA", pid="ddd")
    conn.execute("INSERT INTO yt_jobs (sighting_id, url) VALUES (?,'x')", (sid,))
    stats = scan_youtube.scan(conn, fetch=lambda p: None, sleep=lambda s: None)
    assert stats == {"scanned": 0, "body_hits": 0, "api_hits": 0, "enqueued": 0}
```

- [ ] **Step 2: Run → import error.**
- [ ] **Step 3: Implement `scan_youtube.py`**

```python
"""One-shot retroactive repair: enqueue YouTube downloads for already-ingested
reddit sightings that have no media.

Body links are found in the stored description; link-post URLs were never
persisted, so those posts are re-fetched from the Reddit API (2s throttle —
the script app is shared with ufosarchive). Idempotent: yt_jobs.sighting_id
is UNIQUE and rows with media or an existing job are skipped."""
import time

from app import db, reddit, ytdetect
from app.config import get_settings

API_SLEEP_SECONDS = 2


def scan(conn, *, fetch=None, sleep=time.sleep) -> dict:
    rows = conn.execute(
        """SELECT s.id, s.reddit_post_id, s.description FROM sightings s
           WHERE s.source='reddit'
             AND NOT EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id)
             AND NOT EXISTS (SELECT 1 FROM yt_jobs j WHERE j.sighting_id = s.id)
           ORDER BY s.id""").fetchall()
    stats = {"scanned": len(rows), "body_hits": 0, "api_hits": 0, "enqueued": 0}
    token = None
    for r in rows:
        url = ytdetect.find_in_text(r["description"])
        if url:
            stats["body_hits"] += 1
        elif r["reddit_post_id"]:
            if fetch is not None:
                post = fetch(r["reddit_post_id"])
            else:
                if token is None:
                    token = reddit.script_token()
                post = reddit.fetch_post(token, r["reddit_post_id"])
            sleep(API_SLEEP_SECONDS)
            url = ytdetect.find_youtube_url(post) if post else None
            if url:
                stats["api_hits"] += 1
        if url:
            conn.execute("INSERT OR IGNORE INTO yt_jobs (sighting_id, url) VALUES (?,?)",
                         (r["id"], url))
            stats["enqueued"] += 1
    conn.commit()
    return stats


if __name__ == "__main__":
    c = db.connect(get_settings().db_path)
    try:
        print("scan_youtube:", scan(c))
    finally:
        c.close()
```

- [ ] **Step 4: `pytest -q` green.**
- [ ] **Step 5: Commit** — `git commit -m "feat: retroactive YouTube scan for media-less reddit sightings"`

### Task 6: Local VM worker + systemd units

**Files:**
- Create: `deploy/local-vm/yt_worker.py`, `deploy/local-vm/config.example.json`, `deploy/local-vm/ufosighting-yt.service`, `deploy/local-vm/ufosighting-yt.timer`
- Modify: `deploy/RUNBOOK.md` (worker section)

**Interfaces:**
- Consumes: `ytq.py claim/done/fail` over SSH; R2 via boto3.
- Produces: R2 keys `uploads/%Y/%m/yt_<videoid>_<hex8>.mp4`.

- [ ] **Step 1: Write `yt_worker.py`** (no unit tests — thin orchestration, verified live in Task 7; `python3 -m py_compile` as the syntax gate)

```python
#!/usr/bin/env python3
"""YouTube download worker for ufosighting.report — runs on the LOCAL VM
(192.168.8.224, residential IP; YouTube blocks the Oracle datacenter IP).

Per run (ufosighting-yt.timer every 10 min; Type=oneshot ⇒ never overlaps):
claim pending jobs from Oracle over SSH → yt-dlp each (≤720p mp4, 200MB cap)
→ upload to R2 → report done/fail back over SSH.

Config: ~/ufosighting-yt/config.json (chmod 600) — see config.example.json.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

import boto3

CONFIG_PATH = os.path.expanduser("~/ufosighting-yt/config.json")
YT_DLP = os.path.expanduser("~/.local/bin/yt-dlp")
DOWNLOAD_TIMEOUT = 600
SSH_TIMEOUT = 60


def ssh_ytq(cfg, *args):
    remote = ("cd /home/ubuntu/ufosighting && .venv/bin/python ytq.py "
              + " ".join(shlex.quote(str(a)) for a in args))
    proc = subprocess.run(
        ["ssh", "-i", os.path.expanduser(cfg["oracle_key"]),
         "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         f"{cfg['oracle_user']}@{cfg['oracle_host']}", remote],
        capture_output=True, text=True, timeout=SSH_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh ytq {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def video_id(url):
    m = re.search(r"v=([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else "unknown"


def download(url, out_dir):
    """Run yt-dlp; return the mp4 path or raise RuntimeError.
    yt-dlp exits 0 when --max-filesize skips, so 'no mp4' is also failure."""
    proc = subprocess.run(
        [YT_DLP, "--max-filesize", "200M",
         "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
         "--merge-output-format", "mp4", "--no-playlist",
         "--socket-timeout", "30",
         "-o", os.path.join(out_dir, "video.%(ext)s"), url],
        capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
    mp4 = os.path.join(out_dir, "video.mp4")
    if proc.returncode != 0 or not os.path.exists(mp4) or os.path.getsize(mp4) == 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(tail or "yt-dlp produced no mp4")
    return mp4


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    jobs = json.loads(ssh_ytq(cfg, "claim") or "[]")
    if not jobs:
        return
    s3 = boto3.client("s3", endpoint_url=cfg["r2_endpoint"],
                      aws_access_key_id=cfg["r2_access_key"],
                      aws_secret_access_key=cfg["r2_secret_key"],
                      region_name="auto")
    for job in jobs:
        td = tempfile.mkdtemp(prefix="ufoyt_")
        try:
            mp4 = download(job["url"], td)
            now = datetime.now(timezone.utc)
            key = (f"uploads/{now:%Y}/{now:%m}/"
                   f"yt_{video_id(job['url'])}_{uuid.uuid4().hex[:8]}.mp4")
            size = os.path.getsize(mp4)
            with open(mp4, "rb") as f:
                s3.put_object(Bucket=cfg["r2_bucket"], Key=key, Body=f,
                              ContentType="video/mp4")
            ssh_ytq(cfg, "done", job["job_id"], "--key", key, "--size", size)
            print(f"job {job['job_id']} sighting {job['sighting_id']}: {key} ({size}B)")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            msg = str(exc)[:300]
            print(f"job {job['job_id']} failed: {msg}", file=sys.stderr)
            try:
                ssh_ytq(cfg, "fail", job["job_id"], "--error", msg)
            except RuntimeError as exc2:
                print(f"could not report failure: {exc2}", file=sys.stderr)
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: `config.example.json`**

```json
{
  "oracle_host": "170.9.36.91",
  "oracle_user": "ubuntu",
  "oracle_key": "~/.ssh/oracle2.key",
  "r2_endpoint": "https://<account>.r2.cloudflarestorage.com",
  "r2_access_key": "<key>",
  "r2_secret_key": "<secret>",
  "r2_bucket": "media-ufosighting-report"
}
```

- [ ] **Step 3: systemd units**

`ufosighting-yt.service`:
```ini
[Unit]
Description=ufosighting.report YouTube download worker
After=network-online.target

[Service]
Type=oneshot
User=tom
ExecStart=/usr/bin/python3 /home/tom/ufosighting-yt/yt_worker.py
```

`ufosighting-yt.timer`:
```ini
[Unit]
Description=Run ufosighting YouTube worker every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: RUNBOOK section** — where the worker lives, config path, `sudo systemctl start ufosighting-yt.service` to force a run, `journalctl -u ufosighting-yt` for logs, queue inspection via `ytq.py claim`.
- [ ] **Step 5: `python3 -m py_compile deploy/local-vm/yt_worker.py`; commit** — `git commit -m "feat: local-VM yt-dlp worker + systemd timer"`

### Task 7: Deploy + retroactive scan + live verification

**Files:** none new (ops).

- [ ] **Step 1:** Oracle deploy: `bash deploy/deploy.sh` (rsync + restart; migration is idempotent). Confirm `yt_jobs` exists and web/ingest healthy.
- [ ] **Step 2:** Local VM: `mkdir ~/ufosighting-yt`, copy `yt_worker.py`; build real `config.json` from Oracle's `/home/ubuntu/ufosighting/.env` values; `chmod 600`. Install both units to `/etc/systemd/system/`, `daemon-reload`, `enable --now ufosighting-yt.timer`.
- [ ] **Step 3:** Verify SSH path from local VM: `ssh -i ~/.ssh/oracle2.key ubuntu@170.9.36.91 'cd /home/ubuntu/ufosighting && .venv/bin/python ytq.py claim'` → `[]` or jobs JSON.
- [ ] **Step 4:** On Oracle (after backfill finishes): `.venv/bin/python scan_youtube.py` → note enqueued count.
- [ ] **Step 5:** Force one worker run: `sudo systemctl start ufosighting-yt.service`; check journal; on Oracle confirm jobs `done`, media rows exist, thumbs worker made posters, and the Okinawa (`1upvfrb`) + shorts sightings show video on the site (via VM localhost:8010 — CF challenges bots).
- [ ] **Step 6:** Commit any runbook fixups; push to GitHub.
