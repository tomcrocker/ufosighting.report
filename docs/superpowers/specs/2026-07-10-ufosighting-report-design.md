# ufosighting.report — Design Spec

**Date**: 2026-07-10
**Status**: Draft for review
**Owner**: Tom (u/tmosh, r/UFOs moderator)

## 1. Purpose

A public website at **ufosighting.report** where r/UFOs users submit UFO sighting
reports through a structured form, and anyone can browse a visually rich gallery
of all sightings (media, map, filters, search).

Decisions made during brainstorming:

| Decision | Choice |
|---|---|
| Pipeline direction | **Both, phased**: Phase 1 = site → Reddit; Phase 2 = ingest Sighting-flaired Reddit posts into the gallery |
| Reddit relay | Post created **as the submitting user** via OAuth (`identity` + `submit` scopes) |
| Gallery moderation | **Instant visibility**, then mirror Reddit mod actions (auto-hide on removal, auto-restore on approval) |
| v1 gallery views | Media grid + detail pages, map view, filters, full-text search |
| Stack | **FastAPI + Jinja2 SSR + SQLite (FTS5) on the Oracle VM; media on Cloudflare R2** |

## 2. Architecture

```
Browser ── ufosighting.report ──→ Cloudflare Tunnel ──→ Oracle VM (170.9.36.91)
                                                          ├─ nginx :80 → uvicorn :8010 (FastAPI)
                                                          ├─ SQLite (WAL) + FTS5
                                                          └─ systemd: web service + sync timer
Browser ── media.ufosighting.report ──→ Cloudflare CDN ──→ R2 bucket (public read)
Browser ── presigned PUT ──────────────────────────────→ R2 bucket (direct upload)
```

- **Oracle VM** (Ubuntu 24.04, 1GB RAM + 4GB swap, 28GB free): app at
  `/home/ubuntu/ufosighting/`, DB at `data/sightings.db`, systemd units
  `ufosighting-web.service` + `ufosighting-sync.timer`.
- **The VM never touches media bytes.** Browsers upload directly to R2 via
  presigned URLs; media serves from R2 through Cloudflare's CDN (zero egress
  cost). The only media work on the VM is thumbnail generation (Pillow for
  images, ffmpeg poster-frame for video), one job at a time via an in-app queue.
- **Cloudflare**: add `ufosighting.report` + `www` ingress to the existing
  tunnel on the VM (currently carries only a stale `ufosarchive.xyz` entry —
  verify that DNS moved to the local VM's tunnel before touching it). New R2
  bucket `ufosighting-media`, custom domain `media.ufosighting.report`, CORS
  allowing PUT from the site origin.
- **Two Reddit apps**:
  - New **web app** for visitor OAuth — scopes `identity` + `submit`,
    `duration=temporary` (1h tokens, no refresh tokens, nothing durable stored).
  - Existing **script app** (mod account) for background sync — mod credentials
    see removal state reliably.
- **Git/deploy**: Mac dir `~/dev/claude/ufos-sightings-website/` is the git
  authority with a GitHub remote. `deploy.sh` rsyncs to the VM and restarts the
  service. Commit after every change session so the VM cannot drift from the
  repo (the report-bot-v2 lesson).

## 3. Submission flow (Phase 1) — multi-step wizard

**UX model (inspired by Enigma Labs' submission form):** a full-screen,
one-question-per-screen wizard with a progress bar, Previous/Next navigation,
and a split layout (form on the left, atmospheric visual pane on the right —
CSS starfield; the location step shows a live map instead). Implemented as a
single HTML form whose sections are stepped through with vanilla JS; one POST
at the end. No SPA framework.

0. **Auth gate**: Visitor clicks **Report a sighting** → "Login with Reddit" →
   OAuth consent (`identity` + `submit`) → callback stores username + access
   token in a **server-side session** (opaque HttpOnly cookie, ~1h lifetime).
   Tokens never go into the cookie.

**Wizard steps:**

1. **Where** — location search box with autocomplete (server proxies
   OSM/Nominatim at `GET /api/geocode?q=`; picks fill location text, city,
   country, lat/lon and preview on a Leaflet map) or drop a pin manually.
   Privacy toggle: *"Is this your home or another personally identifiable
   location?"* — if yes, coordinates are **rounded to ~11km (1 decimal)**
   server-side and the stored location text is reduced to city + country.
2. **When** — date + local time (browser timezone auto-detected and shown),
   plus *"How long did the sighting last?"* as h/m/s inputs.
3. **Story** — report title + free-text story (min 150 characters), with
   "questions to consider" prompts (where were you, weather/visibility, how it
   moved, how it disappeared).
4. **The object(s)** — mad-libs style chip pickers:
   - "I saw **[1 / 2 / 3 / 4 / 5+]** objects"
   - "They were **[shape]** shaped" — changing, chevron, cigar, circle, cone,
     cross, cube, cylinder, diamond, disk, egg, fireball, flash, formation,
     light, oval, rectangle, saucer, sphere, teardrop, triangle, unknown
   - "At their closest they were **[distance]**" — very close (&lt;50 ft),
     within a football field, a few miles, as far as the horizon, above the
     trees, as high as a plane, as high as a star
   - "Viewed at arm's length they were **[size]**" — pinhead, pea, dime,
     quarter, golf ball, baseball, grapefruit, basketball, larger
   - "They had **[movement]**" (multi-select) — hovering, floating around,
     straight and steady, circular, slowly descending, unpredictable/erratic,
     random smooth, extremely fast, abrupt changes in direction
5. **Object features** — yes / no / not sure rows: has wings? has rotors? has
   an exhaust plume? makes noise?
6. **Media** — drag-drop direct-to-R2 (presigned PUT; images ≤25MB, video
   ≤500MB, up to 10 files) with per-file progress, or "I didn't capture
   anything".
7. **Witnesses & context** — witness count (including yourself), *"Did any
   sensors detect the object?"* (infrared, night vision, radar, sonar, other,
   none), *"Are you any of the following?"* (active duty military, veteran,
   pilot, scientist, law enforcement, none) — then **Submit**.

Every structured question is **optional except location, date/time, title, and
story** — skippable steps lower abandonment; nulls are fine in the gallery.

On submit: sighting row created → **Reddit post created as the user** with
their token — user's title, body = structured summary + media links + link
back to the gallery page, Sighting flair set → `reddit_post_id` saved → entry
live → redirect to its gallery page.

Token expired mid-wizard: draft saved server-side, user bounces through
re-auth, wizard restored with their answers. No lost 20-minute writeups.

## 4. Data model

```sql
sightings: id, source ('site'|'reddit'), reddit_username, title, description,
           sighted_at (UTC) + tz_name, duration_seconds, shape, witnesses,
           num_objects ('1'|'2'|'3'|'4'|'5+'),
           distance (category), apparent_size (category),
           movement (JSON array of pattern strings),
           has_wings, has_rotors, has_plume, makes_noise ('yes'|'no'|'unsure'|NULL),
           sensors (JSON array), witness_background (JSON array),
           location_text, city, country, lat, lon, location_obscured (bool),
           reddit_post_id, reddit_score, reddit_num_comments,
           status ('live'|'removed_on_reddit'|'deleted_by_user'|'hidden_by_admin'),
           featured (bool), created_at

media:     id, sighting_id, r2_key, thumb_key, kind ('image'|'video'),
           width, height, duration, size_bytes, sort_order

sessions:  id (opaque), username, access_token, expires_at
drafts:    username, form_json, updated_at
sightings_fts: FTS5 virtual table (title, description, location_text),
               kept in sync by triggers
```

- `source` is the Phase-2 seam: Reddit-ingested sightings share the table with
  structured fields null; every view handles them from day one.
- `status` never deletes data — hidden entries keep everything (ufosarchive
  philosophy).

## 5. Gallery views (v1)

- **Home / grid**: thumbnail-first card grid, newest first. Cards show thumb,
  shape badge, location, date, Reddit score. Filter bar: date range, shape,
  country, media type. Clean URLs `/sighting/{id}/{slug}`, sitemap.xml,
  OpenGraph cards per sighting.
- **Map**: Leaflet + OpenStreetMap (free, no API key), marker clustering, same
  filter bar; pin click shows a mini-card linking to the detail page. Only
  sightings with coordinates appear.
- **Detail page**: media carousel (images + `<video>` with poster), structured
  field panel, description, "Discuss on Reddit" link with live score/comment
  count, submitter's username linking to their Reddit profile.
- **Search**: FTS5 across title/description/location, results in the card grid.

## 6. Moderation sync

`ufosighting-sync.timer` every 15 minutes, for entries under 30 days old,
using the script-app (mod) client:

- Post removed by mods/AutoMod → `status = removed_on_reddit` (hidden from
  public views)
- Post deleted by author → `status = deleted_by_user`
- Post restored/approved → back to `live` **automatically** — r/UFOs AutoMod
  removes-then-approves constantly; one-way sync would silently eat legitimate
  sightings
- Otherwise refresh `reddit_score` / `reddit_num_comments`

**Admin**: usernames in an `ADMIN_USERS` env list (logging in through the same
Reddit OAuth) get hide/unhide/feature buttons on detail pages plus an `/admin`
list of recently hidden items. No separate auth system.

## 7. Phase 2 — Reddit ingest

A poller (same script-app client) watches r/UFOs for new **Sighting-flaired
posts** not already present (`reddit_post_id` dedupe skips site-submitted
ones). For each: pull title/selftext, download media — i.redd.it images,
galleries, v.redd.it video+audio mux via ffmpeg (ufosarchive playbook) —
upload to R2, create a `source='reddit'` entry. No structured fields, so they
appear in grid/search/filters but not the map. Subreddit name is an env var so
dev runs against a test subreddit.

## 8. Error handling

- **Upload failures**: client retries each presigned PUT 3×, per-file error
  state; submission blocked until required files resolve.
- **Reddit post failures**: entry goes live only after the Reddit post
  succeeds. 401 → re-auth with draft restore; ratelimit → surface Reddit's
  "try again in X minutes" with the form intact.
- **Orphaned R2 objects** (uploaded, never submitted): daily cleanup deletes
  unreferenced objects older than 48h.
- **Thumbnail failures**: placeholder + one retry; never blocks the sighting.
- **SQLite discipline**: WAL + busy_timeout; only the web app and sync service
  write; short transactions; no long-running readers (archive.db outage
  lesson).

## 9. Testing & dev environment

- **pytest** + FastAPI TestClient: auth flow, presign endpoint, submission with
  mocked Reddit, sync state machine (removed → approved → live transitions).
- **Dev on Mac**: uvicorn + local SQLite + separate dev R2 bucket + a second
  Reddit web app with `http://localhost:8010/auth/callback` redirect, pointed
  at a test subreddit.

## 10. Rollout checklist (Phase 1)

1. Register Reddit web app (prod) + web app (dev); confirm script-app creds
2. Create R2 bucket `ufosighting-media` + custom domain + CORS
3. DNS: `ufosighting.report`, `www`, `media` in Cloudflare
4. Tunnel ingress on the VM; nginx site; systemd units
5. Deploy app; submit test sighting against test subreddit; then flip
   `SUBREDDIT=UFOs`

## 11. Out of scope for v1

- Anonymous (non-Reddit) submissions
- Comments/discussion on the site itself (discussion stays on Reddit)
- Video transcoding (serve uploads as-is)
- Meilisearch (FTS5 is sufficient at this scale)
- Parsing structured fields out of Phase-2 Reddit post text
