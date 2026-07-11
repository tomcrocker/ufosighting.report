# YouTube Media Ingest (yt-dlp) — Design

**Date:** 2026-07-11
**Status:** Approved

## Problem

Sighting posts that host their video on YouTube currently ingest with no media.
Two shapes exist:

1. **Link posts** — `post.url` is a YouTube URL (e.g. sighting 8, a
   `youtube.com/shorts/` link post).
2. **Body links** — self posts with the YouTube URL inside `selftext`
   (e.g. reddit post `1upvfrb`, "Mysterious light seen in the sky over
   Okinawa").

Already-ingested rows need retroactive repair. For body links the URL is in
the stored `description`; for link posts the URL was never persisted, so the
post must be re-fetched from the Reddit API.

## Hard constraint

YouTube blocks downloads from datacenter IPs. The Oracle VM cannot run
yt-dlp. Downloads run on the **local VM 192.168.8.224** (residential IP,
yt-dlp 2026.03.17 + ffmpeg + boto3 installed, `~/.ssh/oracle2.key` present —
same pattern as ufosarchive's `~/yt_download_r2.py`), uploading directly to
R2 and reporting back to Oracle over SSH.

## Architecture

```
Oracle (queue + bookkeeping)                Local VM (downloads)
────────────────────────────                ─────────────────────────────
ingest_post: no reddit media?               yt_worker.py (systemd timer,
  find_youtube_url(post) →                  every 10 min, oneshot = no
  INSERT yt_jobs(pending)                   overlapping runs):
                                              1. ssh oracle: ytq.py claim
scan_youtube.py (one-shot):                      → JSON [{job, sid, url}]
  repair existing media-less rows             2. yt-dlp ≤720p mp4, 200MB cap
                                              3. boto3 put → R2
ytq.py (CLI, run over SSH):                   4. ssh oracle: ytq.py done
  claim / done / fail                            (media row + Meili) or fail
```

## Components

### `app/ytdetect.py` (Oracle)

`find_youtube_url(post: dict) -> str | None` — returns a canonical
`https://www.youtube.com/watch?v=<id>` URL or None.

- Checks `post["url"]` first, then `post["selftext"]`.
- Recognises `youtu.be/<id>`, `youtube.com/watch?v=`, `/shorts/<id>`,
  `/live/<id>`, `/embed/<id>` (with or without `www.`/`m.`/`music.`).
- **First match only** — the sighting video; later body links are usually
  channel promo.
- Ignores channel/user/playlist URLs.
- Also exposes `find_in_text(text: str) -> str | None` for the retroactive
  scan of stored descriptions.
- Video IDs are `[A-Za-z0-9_-]{11}`.

### `yt_jobs` table (Oracle, added to `SCHEMA_TABLES`)

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
CREATE INDEX IF NOT EXISTS idx_yt_jobs_status ON yt_jobs(status);
```

`sighting_id UNIQUE`: max one YouTube video per sighting, and enqueueing is
naturally idempotent (`INSERT OR IGNORE`).

### Ingest hook (`ingest.py`)

In `ingest_post`, when `download_media()` produced nothing, call
`ytdetect.find_youtube_url(post)`; if a URL is found, insert a pending
`yt_jobs` row in the same commit as the sighting row. Reddit-hosted media
always wins — the YouTube path is a fallback only.

### `ytq.py` (Oracle, queue CLI — invoked over SSH by the worker)

Importable functions + argparse CLI. Run as
`cd /home/ubuntu/ufosighting && .venv/bin/python ytq.py <cmd>` (cwd matters:
`.env` loads relative to cwd).

- `claim [--limit N]` (default 5) — print JSON list of
  `{"job_id", "sighting_id", "url"}` for `status='pending' AND attempts < 3`.
  Does not mutate; the single timer-driven worker means no double-claim risk.
- `done <job_id> --key <r2_key> --size <bytes>` — insert
  `media(sighting_id, r2_key, kind='video', sort_order, size_bytes)` with
  `sort_order` = current max+1 for that sighting (defensive; normally 0),
  mark job `done`, then `search.index_sightings([sighting_id])` so the
  gallery card gets its video (thumbs worker in the web app makes the poster
  automatically).
- `fail <job_id> --error <msg>` — `attempts += 1`, store `last_error`
  (truncated to 300 chars); `status='failed'` once attempts reach 3, else
  stays `pending` for the next worker pass.

### `scan_youtube.py` (Oracle, one-shot retroactive repair)

For every `source='reddit'` sighting with zero media rows and no existing
`yt_jobs` row:

1. Try `ytdetect.find_in_text(description)` (body-link case — free).
2. If no match, re-fetch the post via `reddit.fetch_post` (script token) and
   run `find_youtube_url` on the full post dict (link-post case), sleeping
   2s between API calls (shared script-app throttle discipline).
3. Enqueue any hit as a pending job. Print a summary
   (`scanned/body_hits/api_hits/enqueued`).

Safe to re-run any time (idempotent via the UNIQUE constraint).

### Local VM worker (`deploy/local-vm/`)

- `yt_worker.py` — reads `~/ufosighting-yt/config.json` (chmod 600: R2
  endpoint/keys/bucket, Oracle host/user/key path). Per run: SSH `claim`;
  for each job run yt-dlp (ufosarchive's proven flags:
  `--max-filesize 200M -f "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
  --merge-output-format mp4 --no-playlist --socket-timeout 30`, 600s
  subprocess timeout) into a temp dir; on success upload to R2 key
  `uploads/%Y/%m/yt_<videoid>_<hex8>.mp4` and SSH `done`; on any failure
  (including "no mp4 produced" — yt-dlp exits 0 when `--max-filesize` skips)
  SSH `fail` with the error tail. Temp dir removed either way.
- `config.example.json` — template committed; real creds only on the VM.
- `ufosighting-yt.service` (`Type=oneshot`, `User=tom`) +
  `ufosighting-yt.timer` (`OnBootSec=5min`, `OnUnitActiveSec=10min`) —
  oneshot under a timer cannot overlap itself.

## Error handling

- Worker unreachable / VM offline: jobs simply wait as `pending`; nothing
  on Oracle blocks.
- Video deleted/private/oversized: 3 attempts then `failed` with
  `last_error`; the sighting stays media-less with its Reddit link — same
  graceful degradation as today.
- SSH failure mid-run: job stays `pending`, retried next pass; R2 upload
  without a `done` mark would orphan one object, which `cleanup.py`'s
  unreferenced-key sweep already handles.

## Testing

Oracle-side code is unit-tested (pytest, no network):
- `tests/test_ytdetect.py` — URL shapes: link post, shorts, youtu.be,
  body text, m./music. hosts, first-match-wins, channel/playlist ignored.
- `tests/test_ingest.py` — ingest enqueues a job for a body-link post with
  no reddit media; does NOT enqueue when reddit video exists.
- `tests/test_ytq.py` — claim lists pending only; done inserts media +
  marks done; fail increments attempts and flips to failed at 3.
- `tests/test_scan_youtube.py` — body-description hit needs no API call;
  API fallback for link posts; skips rows that already have media/jobs.

Worker script: thin orchestration around yt-dlp/boto3/ssh — verified live
against the real queue during deploy (Okinawa post `1upvfrb` and the
shorts link post are the acceptance cases).

## Deploy

1. Oracle: normal `deploy/deploy.sh` (new files ride along; migration is
   idempotent `CREATE TABLE IF NOT EXISTS`).
2. Local VM: create `~/ufosighting-yt/`, copy worker + real `config.json`
   (creds lifted from Oracle's `.env`), install + enable the timer.
3. Run `scan_youtube.py` on Oracle (after the 30-day backfill finishes),
   trigger the worker once, verify videos appear for the known YouTube
   sightings.
