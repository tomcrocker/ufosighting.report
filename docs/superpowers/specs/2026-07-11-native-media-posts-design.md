# Native Media in Bot Posts — Design

**Date:** 2026-07-11
**Status:** Approved

## Problem

`post_sighting` submits a text-only self post; submitted media appear as bare
URLs in the body. Reddit shows native image/gallery/video posts far more
prominently. The bot should post the media natively.

## Decisions (user-confirmed)

- **Either photos OR video per post, never both.** Video-first: if the
  submission has any video, post the first video natively; otherwise one
  image → `kind=image`, several images → native gallery (cap 20). Media not
  embedded stays reachable via the sighting-page link.
- **Attribution moves to a first comment** — native media posts cannot carry
  body text. The bot comments the current `format_post_body` output (facts,
  description, sighting-page link, "Reported by u/X (verified|self-reported
  via ufosighting.report)"). `media_urls` is left empty in the comment — the
  media is the post itself.
- **Fallback**: any failure before Reddit accepts the submit → today's
  self post. The verify flow never breaks because of media.
- Only `source='site'` submissions are affected (ingested posts are never
  bot-posted). Media-less submissions keep the self-post path unchanged.

## Components

### `app/reddit_media.py` (new — all script-app endpoints)

- `upload_asset(token, filename, mimetype, data: bytes) -> Asset` —
  `POST /api/media/asset.json` for a lease, then multipart-POST the bytes to
  the returned S3 action URL with the lease fields. Returns
  `Asset(asset_id, url)` where `url = action + "/" + key` (the i.redd.it /
  v.redd.it ingest URL). Non-200 at either step → `reddit.RedditError`.
- `submit_image(token, *, subreddit, title, image_url, flair_id) -> None` —
  `/api/submit`, `kind=image`, `url=image_url`. Accepted response carries no
  post id (async creation); errors array is still checked (RATELIMIT →
  `RateLimited`).
- `submit_video(token, *, subreddit, title, video_url, poster_url, flair_id)
  -> None` — `/api/submit`, `kind=video`, `video_poster_url=poster_url`.
  Same async semantics as image.
- `submit_gallery(token, *, subreddit, title, asset_ids, flair_id) -> str` —
  `POST /api/submit_gallery_post.json` with
  `items=[{"media_id": id, "caption": "", "outbound_url": ""}]`. Synchronous:
  returns the post id parsed from the response url/id.
- `find_recent_post_id(token, *, username, title, max_age_s=300) -> str | None`
  — `GET /user/{username}/submitted?limit=10`, newest post whose title
  matches exactly and `created_utc` is within `max_age_s`. Used both for
  polling and for retry dedupe.
- `wait_for_post_id(token, *, username, title, timeout_s, interval_s=3) ->
  str | None` — polls `find_recent_post_id` until found or timeout.
  Image timeout 30s; video 120s (transcode).
- `comment(token, *, post_id, text) -> None` — `POST /api/comment`,
  `thing_id=t3_<id>`. Errors raise `RedditError`; the caller treats comment
  failure as non-fatal.

### `app/posting.py` (rework)

`post_sighting(conn, sighting_id, *, verified)` becomes:

1. Load row + media (now selecting `r2_key, thumb_key, kind`).
2. **Retry dedupe**: `find_recent_post_id(title=row title)` — if the bot
   already posted this title in the last 5 minutes, adopt that post id and
   skip to step 5 (covers "poll timed out, user clicked verify again").
3. Choose the native plan:
   - any `kind='video'` row **with a thumb_key** → video plan (first video;
     poster = thumb public bytes re-uploaded as poster asset)
   - else if ≥1 image → image plan (1 image) or gallery plan (2+, first 20)
   - else (no media, or video without poster) → self-post plan (status quo)
4. Execute the plan inside `try/except RedditError`:
   - download bytes from R2 (`r2.client().get_object`), `upload_asset` each,
     then the matching submit call
   - image/video: `wait_for_post_id`; gallery: id from response
   - **failure before an accepted submit** → fall back to the self-post plan
   - **accepted submit but poll timeout** → raise `RedditError` (no fallback
     — the post is likely still transcoding; the retry-dedupe in step 2
     rescues the next attempt)
5. Native path: `comment()` the formatted body (non-fatal on failure).
   Self-post path: body in the post as today.
6. DB update + `search.index_sightings` unchanged.

### Helpers

`format_post_body` is reused as-is with `media_urls=[]` for the comment.

## Error handling summary

| Failure | Outcome |
|---|---|
| Lease/upload/asset error | fall back to self post |
| Submit rejected (errors array) | RATELIMIT → `RateLimited` raised (caller retries later — status unchanged); other errors → fall back to self post |
| Submit accepted, poll timeout | raise `RedditError`; retry adopts the post via title dedupe |
| Gallery submit error | fall back to self post |
| Comment fails | non-fatal; post stands, details remain on the site |

## Testing

- `tests/test_reddit_media.py` — stubbed httpx: lease+upload happy path and
  HTTP failure; each submit kind (incl. errors array parsing); poll
  found/timeout; comment.
- `tests/test_posting.py` additions — video-first selection when both kinds
  present; single image → `submit_image`; multi-image → gallery; no media →
  self post unchanged; upload failure → self-post fallback; poll timeout →
  raises without fallback; retry adopts existing post id; comment failure
  non-fatal; attribution text present in comment body.
- Live: photo submission + video submission against r/tmoshtest.
