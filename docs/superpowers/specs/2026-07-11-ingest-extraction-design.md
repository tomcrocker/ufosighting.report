# ufosighting.report — Ingest Extraction Design (date / time / location)

**Date**: 2026-07-11
**Status**: Draft for review
**Builds on**: `2026-07-11-anon-submission-bot-ingest-design.md` (Component 4 — ingest)
**Owner**: Tom (u/tmosh, r/UFOs moderator)

## Purpose

The current ingest (`ingest.py`) pulls Sighting-flaired posts from the
subreddit but stores only title + selftext + the post's creation time, with no
location and the wrong date (post time, not sighting time). This makes
ingested entries second-class: no map pin, misleading date.

This rework extracts **structured date / time / location** — plus shape,
duration, and object count where present — from each post, so ingested
sightings are first-class gallery citizens with real map pins and sighting
dates. Reddit sightings state these details inconsistently: in the title, the
selftext, an OP comment, or scattered across all three.

Decisions from brainstorming:

| Decision | Choice |
|---|---|
| Extraction engine | **LLM structured extraction (xAI / Grok)** — one call per post; OpenAI-compatible API |
| Missing fields | **Ingest anyway, best-effort** — null coords ⇒ no pin; no date ⇒ post time |
| Provenance | **Subtle "from r/UFOs" badge** on `source='reddit'` entries |
| Trust | **Validate + clamp in code**, no second LLM call |
| Timezone | LLM returns an **IANA tz**; validate against `zoneinfo`, fall back UTC |
| Reference | rezl/LocationStatementBot (regex/gazetteer) — informs, not copied |

## Pipeline (per post)

```
Sighting-flaired post
 1. fetch post: title, selftext, author, created_utc, media, permalink
 2. fetch OP's top-level comments (location/time often only here)
 3. combine text: title + selftext + OP comments (source-labeled)
 4. xAI/Grok LLM → strict JSON (schema below)
 5. validate_and_clamp(): enforce schema, drop implausible values
 6. geocode location_text → lat/lon (+city/country) via Nominatim (throttled+cached)
 7. derive sighted_at: parsed date + (time | local noon) in the IANA tz → UTC;
    no date ⇒ post created_utc
 8. download media to R2 (existing logic)
 9. INSERT source='reddit' sighting (dedup on reddit_post_id)
```

## Component 1 — `app/extract.py` (LLM extraction)

**Interface:**
- `combine_post_text(post: dict, op_comments: list[str]) -> str` — builds a
  single source-labeled block: `[TITLE] … [BODY] … [OP COMMENT] …`. Truncate
  each source and the total to a sane cap (~6000 chars) to bound token cost.
- `extract_fields(text: str) -> dict` — calls the xAI (Grok) chat-completions endpoint with a
  JSON-only system prompt + the schema, parses the JSON, returns a raw dict.
  On any error (network, non-JSON, missing key) returns `{}` (best-effort).
- `validate_and_clamp(raw: dict, *, post_created_iso: str) -> dict` — pure,
  no network; returns a normalized `extracted` dict.

**LLM output schema** (the prompt asks for exactly this JSON; all fields
nullable):
```json
{
  "date": "YYYY-MM-DD or null",
  "time": "HH:MM 24h or null",
  "timezone": "IANA name e.g. America/Vancouver, or null",
  "location_text": "free text as written, or null",
  "city": "string or null",
  "country": "string or null",
  "shape": "one of the 22 SHAPES or null",
  "num_objects": "1|2|3|4|5+ or null",
  "duration_seconds": "integer or null",
  "summary": "one-sentence neutral summary or null"
}
```

**`validate_and_clamp` rules** (deterministic guardrails):
- `date`: must parse `YYYY-MM-DD` and not be in the future and not before
  1940; else null.
- `time`: must match `HH:MM` 24h; else null.
- `timezone`: must construct via `zoneinfo.ZoneInfo`; else null.
- `shape`: lowercased, must be in `helpers.SHAPES`; else null.
- `num_objects`: must be in `helpers.NUM_OBJECTS`; else null.
- `duration_seconds`: int in `1..86400`; else null.
- `location_text` / `city` / `country`: trimmed strings, capped length; empty ⇒ null.
- `summary`: trimmed, capped ~500 chars.
- Prompt rule: **do not invent** — return null for anything not stated in the
  text (reduces hallucinated locations/dates).

**Config:** `XAI_API_KEY` (required for extraction; empty ⇒ extraction
skipped, ingest still runs best-effort with post-time date), `XAI_MODEL`
(default `grok-3-mini`; configurable — set to whatever current Grok model you
prefer for cost/latency), xAI endpoint
`https://api.x.ai/v1/chat/completions` (OpenAI-compatible, so the client is a
plain `httpx.post` with a Bearer token). Provider is swappable by changing the
base URL + model + key env var — nothing else in the extractor depends on xAI
specifically.

## Component 2 — `app/geocode.py` (shared forward geocoder)

Refactor the Nominatim call currently inline in `app/routes/submit.py` into a
shared module used by both the submit autocomplete endpoint and ingest (DRY).

**Interface:**
- `search(q: str, limit=5) -> list[dict]` — the existing autocomplete behavior
  (list of `{display_name, lat, lon, city, country}`), moved here.
- `forward(q: str) -> dict | None` — single best match for ingest:
  `{lat, lon, city, country, display_name}` or None.
- Module-level throttle: enforce **≥1.1s between outbound Nominatim calls**
  (process-local) to respect their ≤1 req/sec policy; descriptive User-Agent.
- **Cache**: reuse the existing in-memory `_geocode_cache` for autocomplete;
  add a DB-backed cache table `geocode_cache(query TEXT PRIMARY KEY, lat, lon,
  city, country, display_name, cached_at)` so backfill repeats and restarts are
  free and don't re-hit Nominatim. `forward()` checks the DB cache first.

`app/routes/submit.py` `/api/geocode` becomes a thin wrapper over
`geocode.search()` (keeps its existing rate-limit + login-free behavior).

## Component 3 — `ingest.py` (rework)

- Source subreddit is **`INGEST_SUBREDDIT`** (r/UFOs), distinct from
  `SUBREDDIT` (submission target, r/tmoshtest). Default `INGEST_SUBREDDIT` to
  `SUBREDDIT` if unset so nothing breaks.
- `fetch_op_comments(token, post) -> list[str]` — GET the post's comments
  listing, return the OP's top-level comment bodies (author == post author),
  capped (e.g. first 10). One Reddit call per post.
- `build_sighted_at(extracted, post_created_iso) -> (iso_utc, tz_name)`:
  - date present: combine with time (or `12:00` local noon if no time) in the
    extracted tz (or UTC); convert to UTC via `helpers`.
  - date absent: return `post_created_iso`, tz `UTC`.
- `ingest_post(conn, post, token)` — orchestrate: dedup → op comments →
  combine → extract → clamp → geocode (if `location_text`) → sighted_at →
  INSERT with `source='reddit'`, `reddit_username`=author, extracted `shape`,
  `num_objects`, `duration_seconds`, `location_text`, `city`, `country`,
  `lat`, `lon`, `description` = selftext (or LLM `summary` if body empty),
  `sighted_at`, `tz_name`, `reddit_post_id`; then media download (existing).
- Per-post throttle **~2s** (shared Reddit app); geocode throttle handled in
  `geocode.py`.
- `main(backfill)` — timer path = `ingest_once` (recent page); `--backfill`
  walks 30 days (already page-throttled) — bound the backfill to posts newer
  than 30 days (`created_utc` cutoff) rather than all history.

## Component 4 — gallery badge

- `_cards.html` + `detail.html`: when `source == 'reddit'`, render a small
  muted badge "from r/UFOs" on the card and a line on the detail page: "Details
  auto-extracted from the original Reddit post." Links to the Reddit post
  (already have `reddit_post_id`). Purely presentational; no schema change.

## Error handling

- **LLM down / non-JSON / no key**: `extract_fields` returns `{}`; ingest
  proceeds with null structured fields + post-time date. Never aborts.
- **Geocode miss / Nominatim error**: lat/lon stay null (no map pin); entry
  still ingested and appears in grid + search.
- **OP comments fetch fails**: proceed with title + body only.
- **Media download failure**: existing behavior — entry created without that
  media, logged.
- **Dedup**: unchanged — skip any `reddit_post_id` already present (covers
  bot-posted sightings so they aren't double-listed).
- **Rate discipline**: Reddit shared-app throttle (~2s/post + page sleep),
  Nominatim ≥1.1s/call + DB cache; xAI is high-limit. Routine ingest timer
  every 10 min processes only the newest page.

## Testing

- **extract**: `combine_post_text` labels + truncates; `validate_and_clamp`
  drops future date, pre-1940 date, bad time, unknown shape, bad num_objects,
  out-of-range duration, invalid tz; keeps valid ones; `extract_fields`
  returns `{}` on non-JSON / error (mocked xAI HTTP).
- **geocode**: `forward` returns best match (mocked Nominatim); DB cache hit
  skips the network on repeat; throttle is invoked.
- **ingest**: `build_sighted_at` (date+time+tz→UTC; no time→noon; no
  date→post time); `ingest_post` creates a `source='reddit'` row with extracted
  fields + geocoded coords (mocked extract + geocode + reddit + r2); dedup;
  best-effort when extraction empty (null fields, post-time date, still
  ingested); OP-comment text reaches the combiner.
- **gallery**: card + detail show the badge for `source='reddit'` and not for
  `source='site'`.

## Rollout

1. Add `XAI_API_KEY` (+ optional `XAI_MODEL`) and `INGEST_SUBREDDIT=UFOs` to
   the VM `.env`. Keep `SUBREDDIT=tmoshtest`.
2. Deploy; the `geocode_cache` table is created by the idempotent `init_db`.
3. Dry-run: `python ingest.py --backfill` against a SMALL window first (e.g.
   temporarily 2 days) to eyeball extraction quality on real r/UFOs posts;
   inspect a few rows.
4. Run the full 30-day backfill; spot-check map pins + dates in the gallery.
5. Enable `ufosighting-ingest.timer` for ongoing new-post ingest.

## Out of scope

- Re-processing posts when an OP later edits/adds a location comment (best-
  effort once, at ingest time).
- v.redd.it video download/mux (images + galleries only, as today).
- Extracting movement / distance / apparent-size / sensors (site-submission
  fields); ingest fills the high-value subset (date, time, location, shape,
  num_objects, duration). Can extend the schema prompt later if wanted.
- Confidence scoring / second-pass verification.
