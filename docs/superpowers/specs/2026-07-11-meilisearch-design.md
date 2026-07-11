# ufosighting.report — Meilisearch Design ("Meili everywhere", SQL fallback)

**Date**: 2026-07-11
**Status**: Approved in discussion
**Owner**: Tom (u/tmosh)

## Decision

Replace SQLite FTS5 as the search engine and route **all public browsing**
(gallery grid, /search, map pins) through Meilisearch — typo-tolerant search,
facets, and consistent filtering — while keeping **SQLite as an automatic
fallback** on every read path. If Meili is down/unconfigured, the site serves
exactly what it serves today. Dev and CI need no Meili process.

Rationale for the fallback: the 1GB Oracle VM historically OOM'd running
Meilisearch for ufosarchive. Our index is thousands of docs (tiny), but core
browsing must never depend on the Meili process staying alive.

## Deployment (Oracle VM)

- Official Meilisearch binary (latest v1.x) at `/home/ubuntu/meilisearch`,
  systemd unit `meilisearch.service`: `127.0.0.1:7700`, env master key,
  `--db-path /home/ubuntu/ufosighting/data/meili`,
  `--max-indexing-memory 128Mb`, unit `MemoryHigh=250M` / `MemoryMax=300M`,
  `Restart=always`.
- `.env`: `MEILI_URL` (default empty ⇒ Meili disabled, SQL paths only),
  `MEILI_KEY`, `MEILI_INDEX` (default `sightings`).

## Index

Single index `sightings`, primaryKey `id`. Document fields:
`id, title, description, location_text, city, country, reddit_username,
shape, source, status, media_kind (first media kind or null),
sighted_ts (unix int from sighted_at), reddit_score, has_geo (bool)`.

Settings (applied by reindex + on-demand):
- searchableAttributes: title, description, location_text, city, country,
  reddit_username
- filterableAttributes: shape, country, source, status, media_kind,
  sighted_ts, has_geo
- sortableAttributes: sighted_ts, reddit_score
- synonyms: ufo⇄uap⇄uaps, disc⇄disk⇄saucer, tic-tac⇄tictac, orb⇄sphere
- (typo tolerance: Meili defaults)

Only rows in `PUBLIC_STATUSES` are indexed. Meili answers with **ids + counts
(+ facetDistribution)**; cards hydrate from SQLite by id, preserving Meili's
order — SQLite remains the source of truth so scores/statuses render fresh.

## Write-side sync — `app/search.py`

All best-effort (`try/except`, log, never raise into the caller):
- `enabled() -> bool` — MEILI_URL non-empty.
- `index_sightings(conn, ids: list[int])` — read rows, build docs, upsert;
  rows not in PUBLIC_STATUSES are deleted from the index instead.
- `delete_sightings(ids: list[int])`.
- `apply_settings()` — pushes the settings block above.
- Call sites: posting.post_sighting (→live), admin unhide/approve, admin
  hide/reject (delete), ingest_post, sync_once (score/status refresh — reindex
  changed rows), cleanup_pending untouched (pending never indexed).
- `reindex.py` (repo root): apply_settings + walk all PUBLIC rows in batches →
  upsert; `--wipe` recreates the index first. Run once after the backfill.

Client: plain httpx (Authorization: Bearer MEILI_KEY), no SDK. Endpoints used:
`POST /indexes/{i}/documents` (upsert), `POST /indexes/{i}/documents/delete-batch`,
`POST /indexes/{i}/search`, `PATCH /indexes/{i}/settings`, `PUT /indexes` (create).

## Read paths (in `app/routes/public.py`)

New thin layer: try Meili, fall back to SQL on any error/disabled.
- **Gallery `/`**: filters (shape/country/date-range/media_kind) → Meili
  `filter` expression; sort new/old/top(+window) → `sort` on
  sighted_ts/reddit_score (+ sighted_ts window filter); pagination via
  offset/limit; hydrate ids from SQLite. Fallback: existing `query_sightings`.
- **`/search`**: `q` + the same facet filters as dropdowns (shape, country,
  source, date range) with `facets=[shape,country,source]` counts shown in
  the UI; hydrate ids. Fallback: existing FTS5 query (kept intact).
- **`/api/pins`**: `has_geo = true` + date/shape filters, limit 5000, hydrate.
  Fallback: existing SQL.
- FTS5 table + triggers stay (they cost nothing and power the fallback).

## Testing

- `app/search.py`: doc building (media_kind, sighted_ts, has_geo), upsert +
  delete routing by status, settings payload, disabled ⇒ no-ops (respx mocks).
- Read layer: Meili-mocked tests for filter/sort expression building and
  id-order hydration; ALL existing route tests keep passing with Meili
  disabled (they exercise the SQL fallback by construction).
- Deploy: RUNBOOK section (install binary, unit, key, reindex, verify RAM).

## Out of scope

- Geo-radius search on the map (Meili `_geo` — future; `has_geo` bool now).
- Indexing non-public rows or the review queue.
- Replacing gallery country-dropdown source query (cheap SQL, stays).
