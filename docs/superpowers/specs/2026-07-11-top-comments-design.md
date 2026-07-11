# Top 10 Reddit Comments per Sighting — Design

**Date:** 2026-07-11
**Status:** Approved

## Problem

Sighting detail pages show only the post; the discussion (often containing
witness follow-ups and debunks) stays on Reddit. Each sighting should carry
its top 10 upvoted comments.

## Storage

New `comments` table (added to `SCHEMA_TABLES`):

```sql
CREATE TABLE IF NOT EXISTS comments (
  reddit_comment_id TEXT PRIMARY KEY,
  sighting_id INTEGER NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
  author TEXT NOT NULL,
  body TEXT NOT NULL,          -- raw Reddit markdown; rendered via reddit_md
  score INTEGER NOT NULL DEFAULT 0,
  created_utc INTEGER NOT NULL DEFAULT 0,
  permalink TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_comments_sighting ON comments(sighting_id, score DESC);
```

A refresh replaces the sighting's set wholesale (DELETE then INSERT top 10):
scores/rankings stay current, comments deleted on Reddit drop out.

## Fetching — `app/comments.py`

- `fetch_top_comments(token, post_id, *, limit=50) -> list[dict]` —
  `GET oauth.reddit.com/comments/{post_id}?sort=top&depth=1&limit=50`,
  top-level `t1` children only; skips `AutoModerator`, the bot
  (`settings.script_username`), and deleted/empty bodies. Returns dicts
  with id/author/body/score/created_utc/permalink. HTTP failure → `[]`
  (comment refresh is always best-effort).
- `TOP_N = 10`
- `refresh_for_sighting(conn, token, sighting_id, reddit_post_id) -> int` —
  fetch, keep top 10 by score, replace rows, commit; returns count stored.

## Refresh paths

- **Hot sync piggyback** (`sync.py`): after the score/status pass, refresh
  comments for the checked rows that are still `status='live'`, 1s sleep
  between posts. Hot tier ≈ 20 posts/run → ~80 extra calls/hour; full tier
  covers the 30-day window daily. Non-live posts are skipped, so the
  last-fetched comments are preserved as an archive (philosophy: never
  delete on Reddit removal).
- **One-shot backfill** (`backfill_comments.py`): every public sighting with
  a `reddit_post_id`, 2s throttle (~7 min for ~206 rows). Idempotent.

## Display

Detail page section "Top comments on Reddit" under the description: author
(linked), score badge, body via `reddit_md`, timestamp, permalink link on
each comment. Hidden when the sighting has no stored comments. Comments are
NOT indexed in Meilisearch (search stays sighting-level).

## Testing

- `tests/test_comments.py` — respx listing parse (skips AutoMod/bot/deleted,
  caps at 10, orders by score); refresh replaces stale rows; HTTP error → 0
  stored, existing rows kept.
- `tests/test_sync.py` addition — hot sync refreshes comments for live rows
  only, skips removed ones.
- `tests/test_public.py` addition — detail page renders stored comments;
  section absent when none.
- Live: backfill run on prod, spot-check a busy sighting's page.
