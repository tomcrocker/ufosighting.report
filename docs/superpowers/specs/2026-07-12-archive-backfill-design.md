# 12-Month Archive-Fed Backfill — Design

**Date:** 2026-07-12
**Status:** Approved

## Problem

Backfill 12 months of r/UFOs Sighting posts (4,232 currently-visible of
9,840 total; mod-removed and author-deleted are skipped by user decision).
Reddit's search listing caps at ~1000 results, and hammering the OAuth API
for ~8,500 calls is unnecessary: the ufosarchive DB on the dev VM
(192.168.8.224, `/opt/reddit-archive/data/archive.db`) already holds every
post, every comment, and (since 2026-01) downloaded media files under
`/opt/reddit-archive/media/`.

## Architecture — zero OAuth API calls

```
Dev VM (beefy: 31GB RAM, media on disk)        Oracle (light work only)
────────────────────────────────────────       ───────────────────────────
export_sightings.py:                           backfill_archive.py:
  archive.db → per-post JSONL row                for each manifest row:
  (text, op_comments, top_comments)                dedup → xAI extract
  media → R2 bucket media-ufosighting-report:      (archived op_comments)
    2026+: upload archived local files             → geocode → INSERT
    2025:  CDN download (yt-dlp for v.redd.it,     sighting + media keys +
           direct GET i.redd.it/galleries)         top comments + yt_jobs
    youtube: defer → yt_url in manifest            → Meili index
  → scp manifest to Oracle ──────────────────→   (thumbs worker posters
                                                   everything as usual)
```

CDN fetches (v.redd.it, i.redd.it) are public media servers, not the
rate-limited OAuth API. Scores/comment rankings are as-archived — final for
year-old posts.

## Export (`deploy/local-vm/export_sightings.py`, self-contained stdlib+boto3)

- Reads archive.db read-only (`mode=ro` URI — the WAL long-reader lesson:
  queries are short, the media work happens between them).
- Selection: `subreddit='UFOs' AND link_flair_text='Sighting' AND
  created_utc >= now-365d AND COALESCE(removed,0)=0 AND COALESCE(deleted,0)=0`.
- Per post JSONL row: id, title, author, selftext, created_utc, score,
  num_comments, url, op_comments (author's comments, top 10 by score),
  top_comments (top 10 by score, excluding AutoModerator/[deleted]/empty),
  media `[{key, kind}]`, yt_url, media_error.
- Media resolution order:
  1. archive `media` rows with `downloaded=1` → upload
     `/opt/reddit-archive/media/<local_path>` (media_type `video`→video,
     else image); key `uploads/arc/<postid>_<n>.<ext>`.
  2. v.redd.it post URL → yt-dlp (`~/.local/bin/yt-dlp`, ≤720p mp4, 200MB
     cap, reddit extractor handles DASH+mux) → upload.
  3. i.redd.it / direct image URL → GET → upload.
  4. `is_gallery` + `media_metadata` → `https://i.redd.it/<id>.<ext-from-mime>`
     per item → upload (max 20).
  5. YouTube URL (post url or selftext) → no media; `yt_url` in row.
  6. Any failure → `media: []` + `media_error`; the post still ingests
     text-only.
- Politeness: 0.5s between CDN fetches; R2 creds from
  `~/ufosighting-yt/config.json` (already on the VM).
- Resume: on start, reads ids already present in the output JSONL and skips
  them (append mode). Progress line every 25 posts.
- `--days 365 --out PATH --limit N` (limit for smoke tests).

## Oracle ingest (`backfill_archive.py`, in repo, unit-tested)

- `ingest.ingest_post` gains `op_comments: list[str] | None = None` — when
  provided, the per-post comment API fetch is skipped (only refactor to
  shared code).
- Per manifest row: skip if `reddit_post_id` exists → build post-dict
  (title/selftext/author/created_utc/score/num_comments) → extract via
  archived op_comments → geocode → INSERT sighting (`status='live'`,
  date/time/location only, as always) → INSERT media rows from manifest keys
  (sort_order = manifest order) → `yt_url` → `INSERT OR IGNORE yt_jobs` →
  top_comments → `INSERT OR REPLACE comments` → `search.index_sightings`.
- No Reddit sleeps; pace set by xAI (~1-2s) + Nominatim (1.1s, cached).
  Estimated 4-6h for 4,232 posts. Progress every 25; resume = dedup.

## Orchestration

- Export runs `nohup` on the dev VM (`/tmp/export_sightings.log`), est.
  8-14h (1,652 v.redd.it downloads on home bandwidth dominate).
- A chain script on the dev VM waits for export exit, scp's the manifest to
  Oracle (it holds `oracle2.key`), and launches `backfill_archive.py` via
  ssh nohup (`/tmp/backfill12.log`).
- After completion (next session): spot-check counts, run `reindex.py`
  (belt-and-braces), check `yt_jobs` drain, R2/thumbs sanity.
- `cleanup.py` is safe: it only deletes unreferenced `uploads/` keys older
  than 48h; the pipeline references keys well within that.
- The regular ingest timer keeps running concurrently — both sides dedup on
  `reddit_post_id`.

## Costs

~$11 xAI (4,232 extractions), ~40-70GB into R2 (<$1.20/mo), zero OAuth API.

## Testing

- `tests/test_ingest.py`: op_comments passthrough (no fetch when provided).
- `tests/test_backfill_archive.py`: manifest row → sighting + media rows +
  comments + yt job; dedup skip; text-only row; extraction fed op_comments.
- Export script: `py_compile` + live `--limit 3` smoke run on the dev VM
  before the full launch (verifies archive read, yt-dlp, R2 upload, JSONL).
