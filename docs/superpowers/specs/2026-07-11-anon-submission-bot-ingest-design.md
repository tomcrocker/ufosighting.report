# ufosighting.report — Anonymous Submission + Bot Posting + Ingest Design

**Date**: 2026-07-11
**Status**: Draft for review
**Supersedes the submission half of**: `2026-07-10-ufosighting-report-design.md`
**Owner**: Tom (u/tmosh, r/UFOs moderator)

## Why this pivot

Reddit closed self-service OAuth app registration in late 2025; the web-app
approval needed for "post as the logged-in user" is now a multi-week manual
review (request submitted under u/tmosh). Rather than block launch on that, we
switch the submission path to something that works today with a **script app**
under a dedicated bot account (`ufosightingsbot`):

- **Anonymous submission** — no Reddit login. The user types their Reddit
  username manually (self-reported, unverified).
- **Mod-gated bot posting** — submissions wait in a review queue; on mod
  approval, `ufosightingsbot` posts the sighting to the subreddit crediting the
  user in plain text.
- **Ingest** — a poller pulls Sighting-flaired posts from r/UFOs into the
  gallery so it isn't empty and stays current.

The existing OAuth code (`app/reddit_oauth.py`, `app/auth.py` sessions,
`/auth/*` routes) stays in place, dormant, as the future **verified-login**
upgrade: when the web app is approved, logged-in users can post as themselves
and skip the review queue.

Decisions from brainstorming:

| Decision | Choice |
|---|---|
| Post gate | **Mod approval queue first**, then bot posts |
| Attribution | **Plain text "Reported by u/name (self-reported)"**, no @-mention/ping |
| Ingest scope | **Sighting-flaired**, new + historical backfill |
| Anti-spam | **Cloudflare Turnstile** on submit + per-IP rate limit + size caps |
| Username | **Required** |
| Date/time UI | **flatpickr** combined calendar+time popup (CDN, like Leaflet) |

## Status lifecycle (updated)

```
anon submit ──▶ pending_review ──(mod approve → bot posts)──▶ live
                     │                                          │
                     └──(mod reject)──▶ rejected                └──(reddit sync)──▶ removed_on_reddit / deleted_by_user
ingested reddit post ─────────────────────────────────────────▶ live (source='reddit')
admin hide (any) ──▶ hidden_by_admin   (never auto-changed)
```

- New statuses: **`pending_review`** (submitted, awaiting mod approval, no
  Reddit post yet) and **`rejected`** (mod declined; row kept for audit,
  hidden from public).
- `pending_post` from the OAuth design is retired for the anon path (the OAuth
  path may still use it later).
- Only `live` is publicly visible. `pending_review`, `rejected`,
  `hidden_by_admin`, `removed_on_reddit`, `deleted_by_user` are all non-public.

## Component 1 — Anonymous submission

**Routes lose the login gate**: `GET /submit`, `POST /submit`, `POST /api/presign`,
`GET /api/geocode` no longer require a session. `/submit` renders the wizard
directly (no login wall).

**Wizard changes** (`app/templates/submit.html`, `static/js/wizard.js`):
- **flatpickr** replaces the separate `<input type=date>` + `<input type=time>`
  with one field that opens a calendar + time picker popup
  (`enableTime: true`). Loaded from CDN. Writes to hidden `sighted_date` +
  `sighted_time` fields so the existing server validation is unchanged.
- **Reddit username** field, **required**, on the story step: "Your Reddit
  username — we'll credit you (self-reported)." Validated against Reddit's
  username rules (3–20 chars, `[A-Za-z0-9_-]`), stored in the existing
  `reddit_username` column. A leading `u/` is stripped.
- **Cloudflare Turnstile** widget on the final step; its token is submitted
  with the form.
- Location autocomplete already exists (debounced Nominatim → suggestions +
  map pin); it now works because the flow is no longer login-gated.

**CSRF for anonymous**: no session to bind to. Use a **double-submit cookie** —
`GET /submit` sets a random `csrf` cookie and embeds the same value as a hidden
field; `POST /submit` requires they match. Cheap, stateless, standard for anon
forms.

**POST /submit flow**:
1. Verify Turnstile token server-side (`app/turnstile.py`). Fail → 400 re-render.
2. CSRF double-submit check. Fail → 403.
3. Per-IP rate limit (see below). Over limit → 429 re-render.
4. Validate submission (existing `validate_submission` + username validation).
   Verify each media key exists in R2 (`head_exists`).
5. Insert sighting as **`pending_review`** + media rows (store `submitter_ip`).
   No Reddit post.
6. Render a "Thanks — a moderator will review your sighting shortly" page.

**Rate limiting** (`app/ratelimit.py`): single-worker uvicorn, so a small
DB-backed check is simplest and survives restarts — count events from an IP
within a window, keyed by action. Defaults: **5 submissions/hour/IP**,
**40 presign calls/hour/IP**, **60 geocode calls/hour/IP** (the geocode proxy
is public and Nominatim rate-limits our origin, so it needs its own cap on top
of the existing in-memory result cache). Configurable via env.

**Anon upload abuse**: `/api/presign` is now public. Mitigations: per-IP
presign rate limit, existing size caps (image ≤25MB, video ≤500MB, ≤10 files),
and the existing daily cleanup that deletes unreferenced `uploads/` objects
older than 48h. Turnstile gates the final submit, not each upload; junk
uploads that never reach a submitted sighting are swept by cleanup.

## Component 2 — Mod review → bot posting

**Review queue** (`app/routes/admin.py`, new `app/templates/review.html`):
- `GET /admin/review` — lists `pending_review` sightings (admin-only via
  existing `require_admin`), each showing all fields + media thumbnails, with
  **Approve** and **Reject** buttons (CSRF-protected forms).
- `POST /admin/review/{id}/approve` — calls the bot to post, then sets `live`.
- `POST /admin/review/{id}/reject` — sets `rejected` (kept for audit).
- The existing `/admin` page links to the review queue and shows a pending
  count.

**Bot posting** (`app/reddit.py`):
- `ufosightingsbot` obtains a token via the existing `script_token()` (password
  grant). We **consolidate credentials**: the `SCRIPT_*` env vars now hold
  `ufosightingsbot`'s script-app + account creds and drive posting, ingest, and
  the moderation sync.
- `submit_post()` already accepts any bearer token — approval calls it with the
  bot token. Title = the user's title; body = structured summary +
  **"Reported by u/name (self-reported via ufosighting.report)"** as plain text
  (no `u/` link that pings) + gallery link; Sighting flair set.
- On success: save `reddit_post_id`, status → `live`. On `RateLimited` /
  `RedditError`: keep `pending_review`, surface the error on the review page so
  the mod can retry. (No rollback — the submission already exists; only the
  Reddit post is retried.)

**Bot account prerequisite**: `ufosightingsbot` must be a moderator or
approved submitter of the target subreddit, or AutoMod's account-age rule will
remove its posts. Operator responsibility (documented in the runbook).

## Component 3 — Ingest poller

`ingest.py` (repo root, run by a new `ufosighting-ingest.timer`, e.g. every
10 min), using the consolidated `SCRIPT_*` bot creds:

- Fetch recent **Sighting-flaired** posts from r/UFOs via
  `/r/{sub}/search?q=flair_name:"Sighting"&restrict_sr=1&sort=new` (or listing +
  flair filter).
- **Dedup**: skip any `reddit_post_id` already present (covers bot-posted
  sightings — they're already `live` from the approval flow).
- For each new post: pull title/selftext, download media (i.redd.it images,
  Reddit galleries, v.redd.it video+audio muxed via ffmpeg — the ufosarchive
  playbook), upload to R2, create a **`source='reddit'`** sighting with
  structured fields null (they aren't in free-text posts), status `live`.
- **Backfill**: a one-shot mode (`ingest.py --backfill`) walks historical
  Sighting posts once at launch; the timer handles new ones after.
- Ingested posts participate in the existing moderation sync (removed on
  Reddit → hidden) since they have a `reddit_post_id`.

## Data model changes

```sql
ALTER TABLE sightings ADD COLUMN submitter_ip TEXT;         -- rate limit / abuse
ALTER TABLE sightings ADD COLUMN username_verified INTEGER NOT NULL DEFAULT 0;
-- status now also takes 'pending_review' and 'rejected' (TEXT column, no enum change needed)
```

`username_verified` is 0 for anon (self-reported); reserved for the future
OAuth path to mark 1. New installs get these columns in `SCHEMA`; the live DB
gets an idempotent migration in `init_db` (ADD COLUMN guarded by a PRAGMA
check).

## Config additions (`.env`)

```
# ufosightingsbot script app (posting + ingest + mod sync) — was SCRIPT_*
SCRIPT_CLIENT_ID / SCRIPT_CLIENT_SECRET / SCRIPT_USERNAME / SCRIPT_PASSWORD

# Cloudflare Turnstile
TURNSTILE_SITE_KEY=      # public, embedded in the form
TURNSTILE_SECRET_KEY=    # server-side verify

# Rate limits (optional; defaults in code)
RATE_SUBMIT_PER_HOUR=5
RATE_PRESIGN_PER_HOUR=40
RATE_GEOCODE_PER_HOUR=60
```

## Error handling

- **Turnstile down / fails**: submission rejected with a clear message; the
  wizard keeps entered values (existing `show_all` re-render).
- **Bot post fails on approval**: stays `pending_review`, error shown, mod
  retries; no data lost.
- **Ingest media download failure**: create the entry without that media (log
  it); never abort the whole run for one bad post. Thumbnail worker + cleanup
  handle the rest.
- **Rate limit**: 429 with "you've submitted several sightings recently, please
  try again later."
- **SQLite discipline** unchanged (WAL, busy_timeout, only web + timers write).

## Testing

- **pytest**: anon submit happy-path (pending_review created, no Reddit call),
  username validation + `u/` strip, Turnstile verify mocked (pass + fail),
  CSRF double-submit, rate-limit trip, approve → bot posts (mocked) → live with
  reddit_post_id, reject → rejected, review-queue admin gating.
- **Ingest**: dedup skips existing reddit_post_id, new post creates
  `source='reddit'` entry, media-download failure is non-fatal (mocked Reddit +
  R2).
- **flatpickr / Turnstile / autocomplete**: browser pass with Playwright after
  unit tests pass (drive the calendar popup, confirm autocomplete works anon).

## Rollout

1. Reuse a grandfathered script app or `ufosightingsbot`'s own once available;
   add `ufosightingsbot` as a mod/approved submitter of r/tmoshtest (then
   r/UFOs).
2. Create a Cloudflare Turnstile widget → site + secret keys into `.env`.
3. Deploy; add `ufosighting-ingest.timer`; run `ingest.py --backfill` once
   against r/tmoshtest, then flip to r/UFOs.
4. End-to-end test on r/tmoshtest: anon submit → review queue → approve → bot
   post appears as `ufosightingsbot` crediting the user → gallery live →
   thumbnail + map; separately, native Sighting post → ingested, deduped.

## Out of scope (unchanged)

- OAuth "post as self" stays dormant until the web app is approved (then it
  becomes the verified path; anon remains for non-logged-in users).
- No site-side comments; discussion stays on Reddit.
- No structured-field parsing out of ingested free-text posts.
