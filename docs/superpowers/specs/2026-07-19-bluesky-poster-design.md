# Bluesky auto-poster — design

**Goal:** Auto-post new public sightings (with media) to the Bluesky account
`ufosighting.bsky.social` — native image embed, sighting info, link back, hashtags.

## Scope decision (user)
"Media + light quality gate": post newly-ingested r/UFOs sightings AND verified
site submissions that have media and pass a basic quality gate. Rate-limited.
Forward-only (existing ~12,500 sightings are NOT posted). Public only.

## Trigger
A **sweep** `bsky.post_new(conn, limit=N)` called at the END of `ingest_once`
(after all commits + IndexNow), NOT inside any per-row write transaction — never
hold the SQLite write lock across a network call (backfill-incident lesson). The
sweep catches both ingested sightings and verified submissions (any eligible row).
Best-effort: failures logged, never break ingest.

## Eligibility
`bsky_posted_at IS NULL` AND `status='live'` AND has ≥1 media row AND
(`lat IS NOT NULL` OR `length(description) >= 80`). Newest first, up to N/run,
short sleep between posts. A per-row failure leaves `bsky_posted_at` NULL → retried
next sweep.

## Post format (≤300 graphemes)
- **Image embed:** first media's R2 thumbnail (`thumb_key`) fetched (≤1MB JPEG),
  uploaded as a blob, embedded as one image (alt = title). Videos post their poster.
  On image failure: post text-only (link still clickable via facet).
- **Text:**
  ```
  {title}
  📍 {location} · 📅 {date}[ · {Shape}]
  {host}/sighting/{id}/{slug}
  #UFO #UAP #UFOsighting [#Shape] [#Country]
  ```
- **Facets:** the URL (link) and each hashtag (tag) as rich-text facets with correct
  UTF-8 **byte** offsets, computed left-to-right with a moving cursor (so `#UFO`
  doesn't collide with `#UFOsighting`, and emoji byte-widths are handled).
- Title truncated with `…` if the whole thing exceeds the limit.

## DB
Add `bsky_posted_at TEXT` to `_MIGRATION_COLUMNS`. NULL = eligible; ISO ts = posted;
`'skipped'` = marked at rollout (existing rows).

## Module `app/bsky.py`
`enabled()`, `create_session()`, `upload_blob()`, `build_post_text(row)`,
`_hashtags(row)`, `_facets(text,url,tags)`, `_thumb_key(conn,id)`,
`eligible_rows(conn,limit)`, `post_sighting(conn,row,session=)`, `post_new(conn,limit)`.
Config: `BSKY_ENABLED` / `BSKY_HANDLE` / `BSKY_APP_PASSWORD` — off/empty in dev + tests.

## Rollout (forward-only)
1. Deploy code + migration (auto on startup).
2. `UPDATE sightings SET bsky_posted_at='skipped' WHERE bsky_posted_at IS NULL` — mark
   all existing sightings handled (no firehose).
3. Set `BSKY_*` in VM `.env` (app password is a secret — 600, gitignored, revocable).
4. `scripts/bsky_test.py` posts ONE recent eligible sighting to eyeball on the account.
5. After eyeball approval, flip `BSKY_ENABLED=1` → new eligible sightings post at
   ingest cadence (~10 min), rate-capped.

## Non-goals (v1)
Native video upload (poster frame only), description snippet in text, per-monitor
opt-out, backfill of existing sightings.
