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
  username manually.
- **Reddit-inbox verification (fast-lane)** — right after submit,
  `ufosightingsbot` DMs the named user a unique, unguessable verify link on our
  domain. Clicking it proves they control that account (only the account owner
  sees their inbox) → the sighting skips the mod queue and posts immediately.
  This is the "verify your email" pattern applied to a Reddit inbox: it gives
  us OAuth-grade identity proof without the OAuth app.
- **Mod-gated bot posting (fallback)** — submissions that aren't verified
  within the window fall into a review queue; on mod approval,
  `ufosightingsbot` posts the sighting crediting the user in plain text.
- **Ingest** — a poller pulls Sighting-flaired posts from r/UFOs into the
  gallery so it isn't empty and stays current.

The existing OAuth code (`app/reddit_oauth.py`, `app/auth.py` sessions,
`/auth/*` routes) stays in place, dormant, as the future **verified-login**
upgrade: when the web app is approved, logged-in users can post as themselves
and skip the review queue.

Decisions from brainstorming:

| Decision | Choice |
|---|---|
| Identity | **Reddit-inbox verify link** (bot DMs a random link) → verified fast-lane |
| Verified attribution | **"Reported by u/name (verified)"** once the link is clicked |
| Unverified fallback | Falls to **mod approval queue** after the window; mod approves → bot posts |
| Unverified attribution | **Plain text "Reported by u/name (self-reported)"**, no @-mention/ping |
| Ingest scope | **Sighting-flaired**, new + historical backfill |
| Anti-spam | **Cloudflare Turnstile** on submit + per-IP rate limit + size caps |
| Username | **Required** |
| Date/time UI | **flatpickr** combined calendar+time popup (CDN, like Leaflet) |
| Verify token | `secrets.token_urlsafe(32)` — random, single-use, time-limited |
| DM abuse guard | Rate-limit DMs per target username; neutral dismissable wording |

## Status lifecycle (updated)

```
anon submit ──▶ pending_verify ──(user clicks DM link → bot posts)──────▶ live (verified)
                     │                                                     │
                     └──(window elapses, not verified)──▶ pending_review   │
                                                             │             │
                              (mod approve → bot posts) ─────┘──────────▶ live (self-reported)
                                                             │
                              (mod reject) ─────────────────▶ rejected
ingested reddit post ──────────────────────────────────────────────────▶ live (source='reddit')
live ──(reddit sync)──▶ removed_on_reddit / deleted_by_user
admin hide (any) ──▶ hidden_by_admin   (never auto-changed)
```

- New statuses: **`pending_verify`** (submitted, DM sent, awaiting the user to
  click the verify link — not yet posted), **`pending_review`** (verify window
  elapsed, awaiting mod approval), and **`rejected`** (mod declined; row kept
  for audit).
- `pending_post` from the OAuth design is retired for the anon path (the OAuth
  path may still use it later).
- Only `live` is publicly visible. `pending_verify`, `pending_review`,
  `rejected`, `hidden_by_admin`, `removed_on_reddit`, `deleted_by_user` are all
  non-public.

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
5. Insert sighting as **`pending_verify`** + media rows (store `submitter_ip`,
   a random `verify_token`, and `verify_sent_at`). No Reddit post yet.
6. Fire the verification DM (Component 2) unless that username was DM'd too
   recently (per-username guard) — DM failure is logged, non-fatal.
7. Render a "Check your Reddit inbox — we sent u/name a link to confirm it was
   you. (A moderator will review it if you don't.)" page.

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

## Component 2 — Reddit-inbox verification (fast-lane)

**Sending the DM** (`app/reddit.py` + `app/verify.py`): on submit, generate
`verify_token = secrets.token_urlsafe(32)`; `ufosightingsbot` sends a PM via
`/api/compose` to the named user:

> Did you submit a UFO sighting on ufosighting.report? Confirm it was you:
> https://ufosighting.report/verify/&lt;token&gt;
> If this wasn't you, you can ignore this message.

Guards: skip the DM if that username was DM'd within the last hour
(per-username rate limit, prevents weaponized spam); wording is neutral and
dismissable; a failed/silently-dropped DM is non-fatal (the fallback timer
still routes it to mod review).

**Clicking the link** (`app/routes/verify.py`):
- `GET /verify/{token}` — look up the `pending_verify` sighting by token. If
  found, unexpired, and still `pending_verify`: set `username_verified=1`, post
  via the bot (Component 3 posting), status → `live`, credit
  **"(verified via ufosighting.report)"**. Render a "You're verified — your
  sighting is live" page (link to it). Unknown/expired/used token → a friendly
  "this link is no longer valid" page (200, not an error).
- Token is single-use (cleared on success) and unguessable, so the link can't
  be brute-forced to verify someone else's submission.

**Fallback timer** (`app/routes/... via the review/sweep job`): a periodic
sweep (folded into the existing sync timer, or its own) moves `pending_verify`
sightings older than the **verify window (default 6h)** to `pending_review`,
so unverified submissions land in the mod queue instead of sitting forever.

## Component 3 — Mod review → bot posting

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
- `submit_post()` already accepts any bearer token — both the verify fast-lane
  and mod approval call it with the bot token. Title = the user's title; body =
  structured summary + attribution + gallery link; Sighting flair set. The
  attribution line reflects the path: **"Reported by u/name (verified via
  ufosighting.report)"** for the verify fast-lane, **"(self-reported via
  ufosighting.report)"** for mod-approved. Plain text, no `u/` link that pings.
- On success: save `reddit_post_id`, status → `live`. On `RateLimited` /
  `RedditError`: keep the prior status (`pending_verify` or `pending_review`),
  surface the error (review page for mod path; a retry-later page for verify
  path), no rollback — only the Reddit post is retried.

**Bot account prerequisite**: `ufosightingsbot` must be a moderator or
approved submitter of the target subreddit, or AutoMod's account-age rule will
remove its posts. Operator responsibility (documented in the runbook).

## Component 4 — Ingest poller

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
ALTER TABLE sightings ADD COLUMN submitter_ip TEXT;          -- rate limit / abuse
ALTER TABLE sightings ADD COLUMN username_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sightings ADD COLUMN verify_token TEXT;          -- unguessable, single-use
ALTER TABLE sightings ADD COLUMN verify_sent_at TEXT;        -- for the fallback window
CREATE INDEX IF NOT EXISTS idx_sightings_verify_token ON sightings(verify_token);
-- status column (TEXT) now also takes 'pending_verify', 'pending_review', 'rejected'
```

`username_verified` is 1 once the inbox link is clicked (or, later, via OAuth),
0 otherwise. `verify_token` is cleared once used. New installs get these
columns in `SCHEMA`; the live DB gets an **idempotent migration** in `init_db`
(each `ADD COLUMN` guarded by a `PRAGMA table_info` check) so the already-
deployed prod database picks them up on the next restart without a manual step.

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

# Verification
VERIFY_WINDOW_HOURS=6         # unverified → mod queue after this
VERIFY_DM_PER_USERNAME_HOURS=1 # don't DM the same username more than once/window
```

## Error handling

- **Turnstile down / fails**: submission rejected with a clear message; the
  wizard keeps entered values (existing `show_all` re-render).
- **Verification DM fails / spam-filtered**: non-fatal — the sighting still sits
  `pending_verify` and the fallback window routes it to mod review, so a
  dropped DM never strands a submission.
- **Verify link unknown/expired/reused**: friendly "no longer valid" page (200),
  not an error; the submission remains in whatever state it's in.
- **Bot post fails (verify or approval)**: keeps the prior status, error shown,
  retried; no data lost.
- **Ingest media download failure**: create the entry without that media (log
  it); never abort the whole run for one bad post. Thumbnail worker + cleanup
  handle the rest.
- **Rate limit**: 429 with "you've submitted several sightings recently, please
  try again later."
- **SQLite discipline** unchanged (WAL, busy_timeout, only web + timers write).

## Testing

- **pytest**: anon submit happy-path (`pending_verify` created, DM attempted,
  no post yet), username validation + `u/` strip, Turnstile verify mocked
  (pass + fail), CSRF double-submit, rate-limit trip, per-username DM guard.
- **Verify**: valid token → `username_verified=1` + bot posts (mocked) → live;
  expired/unknown/reused token → friendly no-op; fallback sweep moves stale
  `pending_verify` → `pending_review`.
- **Mod review**: approve → bot posts (mocked) → live with reddit_post_id +
  self-reported attribution; reject → rejected; admin gating on `/admin/review`.
- **Ingest**: dedup skips existing reddit_post_id, new post creates
  `source='reddit'` entry, media-download failure is non-fatal (mocked Reddit +
  R2).
- **Browser pass** (Playwright): flatpickr calendar popup, location
  autocomplete anon, Turnstile widget renders, full submit → verify page.
- **flatpickr / Turnstile / autocomplete**: browser pass with Playwright after
  unit tests pass (drive the calendar popup, confirm autocomplete works anon).

## Rollout

1. Reuse a grandfathered script app or `ufosightingsbot`'s own once available;
   add `ufosightingsbot` as a mod/approved submitter of r/tmoshtest (then
   r/UFOs). The script app needs the `submit`, `privatemessages`, and `read`
   scopes (post, send/read DMs, read listings). Let the bot **age / earn a
   little karma** before production so its verify DMs aren't spam-filtered.
2. Create a Cloudflare Turnstile widget → site + secret keys into `.env`.
3. Deploy; add `ufosighting-ingest.timer`; run `ingest.py --backfill` once
   against r/tmoshtest, then flip to r/UFOs.
4. End-to-end test on r/tmoshtest:
   - **Verify path**: anon submit → bot DMs your test account a verify link →
     click it → bot posts crediting you (verified) → gallery live → thumbnail +
     map.
   - **Fallback path**: submit a username you don't control / don't click →
     after the window it lands in `/admin/review` → approve → bot posts.
   - **Ingest**: native Sighting post on r/tmoshtest → ingested, deduped
     against bot-posted ones.

## Out of scope (unchanged)

- OAuth "post as self" stays dormant until the web app is approved (then it
  becomes the verified path; anon remains for non-logged-in users).
- No site-side comments; discussion stays on Reddit.
- No structured-field parsing out of ingested free-text posts.
