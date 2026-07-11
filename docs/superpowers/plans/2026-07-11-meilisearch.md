# Meilisearch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route gallery, /search, and map pins through Meilisearch (typo-tolerant search + facets) with SQLite as an automatic fallback on every path.

**Architecture:** New `app/search.py` (httpx Meili client: docs, upsert/delete, settings, query) + a `meili_query()` read layer in `app/routes/public.py` that tries Meili and falls back to the existing SQL. Write points call `search.index_sightings()`/`delete_sightings()` best-effort. `reindex.py` rebuilds. Meili runs as a systemd service on the VM with hard memory caps.

**Tech Stack:** Meilisearch v1.x binary, httpx, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-07-11-meilisearch-design.md`

## Global Constraints

- `MEILI_URL` empty ⇒ Meili fully disabled: write hooks no-op, reads use SQL. All existing tests must pass with it unset.
- Meili returns ids; cards hydrate from SQLite preserving Meili order (SQLite = source of truth).
- Only `PUBLIC_STATUSES` rows are ever in the index; a row leaving that set is deleted from the index.
- Write hooks are best-effort — a Meili failure must never break a submit/approve/ingest/sync.
- httpx only (no Meili SDK). Tests mock with respx.
- VM unit: `MemoryHigh=250M`, `MemoryMax=300M`, `--max-indexing-memory 128Mb`.
- Run tests `.venv/bin/pytest -q`; commit per task.

## File Structure

```
app/config.py        # + meili_url, meili_key, meili_index
app/search.py        # NEW client: enabled, build_doc, index_sightings, delete_sightings,
                     #   apply_settings, search_ids (query builder)
app/routes/public.py # meili-first read layer + facets on /search
app/routes/admin.py  # index/delete hooks on approve/reject/hide/unhide
app/posting.py       # index hook after live
ingest.py            # index hook after ingest_post
sync.py              # index hook for refreshed rows
reindex.py           # NEW full rebuild (--wipe)
app/templates/search.html  # facet dropdowns + counts
deploy/meilisearch.service, RUNBOOK, .env.example
tests/test_search.py (new), tests/test_public.py (+meili-mocked reads)
```

---

### Task 1: Config + `app/search.py` core (docs, upsert/delete, settings)

**Files:** Modify `app/config.py`, `.env.example`; Create `app/search.py`; Test `tests/test_config.py`, `tests/test_search.py`.

**Interfaces:**
- `Settings`: `meili_url: str` (default ""), `meili_key: str` (""), `meili_index: str` ("sightings").
- `search.enabled() -> bool`; `search.build_doc(row, media_kind) -> dict` (fields per spec; `sighted_ts` int epoch from `sighted_at`, `has_geo` bool); `search.index_sightings(conn, ids)` (public rows upserted, non-public deleted); `search.delete_sightings(ids)`; `search.apply_settings()`; module constants `SYNONYMS`, `SETTINGS`.

- [ ] Tests:
```python
# tests/test_search.py
import httpx, respx
from app import search
from app.config import get_settings
from tests.test_public import seed

MEILI = "http://127.0.0.1:7700"


def _enable(monkeypatch):
    monkeypatch.setenv("MEILI_URL", MEILI)
    monkeypatch.setenv("MEILI_KEY", "masterkey")
    get_settings.cache_clear()


def test_disabled_is_noop(client, app_db):
    # test env has no MEILI_URL -> every hook is a silent no-op
    assert search.enabled() is False
    search.index_sightings(app_db, [1, 2])       # must not raise / not call network
    search.delete_sightings([1])


@respx.mock
def test_index_upserts_public_rows(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db, title="Meili doc", shape="sphere", reddit_score=7)
    up = respx.post(f"{MEILI}/indexes/sightings/documents").mock(
        return_value=httpx.Response(202, json={"taskUid": 1}))
    search.index_sightings(app_db, [sid])
    assert up.called
    import json
    docs = json.loads(up.calls[0].request.content)
    d = docs[0]
    assert d["id"] == sid and d["shape"] == "sphere" and d["has_geo"] is True
    assert isinstance(d["sighted_ts"], int) and d["reddit_score"] == 7


@respx.mock
def test_index_deletes_nonpublic_rows(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db, title="Hidden", status="hidden_by_admin")
    dele = respx.post(f"{MEILI}/indexes/sightings/documents/delete-batch").mock(
        return_value=httpx.Response(202, json={"taskUid": 2}))
    search.index_sightings(app_db, [sid])
    assert dele.called


@respx.mock
def test_meili_failure_never_raises(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db)
    respx.post(f"{MEILI}/indexes/sightings/documents").mock(
        side_effect=httpx.ConnectError("down"))
    search.index_sightings(app_db, [sid])  # must swallow


@respx.mock
def test_apply_settings_payload(monkeypatch):
    _enable(monkeypatch)
    respx.put(f"{MEILI}/indexes").mock(return_value=httpx.Response(202, json={}))
    patch = respx.patch(f"{MEILI}/indexes/sightings/settings").mock(
        return_value=httpx.Response(202, json={}))
    search.apply_settings()
    import json
    body = json.loads(patch.calls[0].request.content)
    assert "sighted_ts" in body["sortableAttributes"]
    assert "has_geo" in body["filterableAttributes"]
    assert "uap" in body["synonyms"]["ufo"]
```
- [ ] Implement config fields + `app/search.py`:
```python
import json
import time
from datetime import datetime, timezone

import httpx

from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
PUBLIC_STATUSES = ("live", "deleted_by_user", "removed_on_reddit")
SYNONYMS = {
    "ufo": ["uap", "uaps"], "uap": ["ufo", "ufos"],
    "disc": ["disk", "saucer"], "disk": ["disc", "saucer"], "saucer": ["disc", "disk"],
    "tic-tac": ["tictac"], "tictac": ["tic-tac"],
    "orb": ["sphere"], "sphere": ["orb"],
}
SETTINGS = {
    "searchableAttributes": ["title", "description", "location_text", "city",
                             "country", "reddit_username"],
    "filterableAttributes": ["shape", "country", "source", "status", "media_kind",
                             "sighted_ts", "has_geo"],
    "sortableAttributes": ["sighted_ts", "reddit_score"],
    "synonyms": SYNONYMS,
}


def enabled() -> bool:
    return bool(get_settings().meili_url)


def _base():
    s = get_settings()
    return s.meili_url.rstrip("/"), {"Authorization": f"Bearer {s.meili_key}"}, s.meili_index


def build_doc(row, media_kind) -> dict:
    try:
        ts = int(datetime.strptime(row["sighted_at"], ISO)
                 .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        ts = 0
    return {
        "id": row["id"], "title": row["title"], "description": row["description"],
        "location_text": row["location_text"], "city": row["city"],
        "country": row["country"], "reddit_username": row["reddit_username"],
        "shape": row["shape"], "source": row["source"], "status": row["status"],
        "media_kind": media_kind, "sighted_ts": ts,
        "reddit_score": row["reddit_score"],
        "has_geo": row["lat"] is not None and row["lon"] is not None,
    }


def index_sightings(conn, ids) -> None:
    if not enabled() or not ids:
        return
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM sightings WHERE id IN ({marks})", list(ids)).fetchall()
        docs, dead = [], []
        for row in rows:
            if row["status"] in PUBLIC_STATUSES:
                mk = conn.execute(
                    "SELECT kind FROM media WHERE sighting_id=? ORDER BY sort_order LIMIT 1",
                    (row["id"],)).fetchone()
                docs.append(build_doc(row, mk["kind"] if mk else None))
            else:
                dead.append(row["id"])
        url, headers, index = _base()
        if docs:
            httpx.post(f"{url}/indexes/{index}/documents", headers=headers,
                       json=docs, timeout=10)
        if dead:
            httpx.post(f"{url}/indexes/{index}/documents/delete-batch",
                       headers=headers, json=dead, timeout=10)
    except httpx.HTTPError as exc:
        print(f"meili index failed: {exc}")


def delete_sightings(ids) -> None:
    if not enabled() or not ids:
        return
    try:
        url, headers, index = _base()
        httpx.post(f"{url}/indexes/{index}/documents/delete-batch",
                   headers=headers, json=list(ids), timeout=10)
    except httpx.HTTPError as exc:
        print(f"meili delete failed: {exc}")


def apply_settings() -> None:
    if not enabled():
        return
    url, headers, index = _base()
    httpx.put(f"{url}/indexes", headers=headers,
              json={"uid": index, "primaryKey": "id"}, timeout=10)
    httpx.patch(f"{url}/indexes/{index}/settings", headers=headers,
                json=SETTINGS, timeout=30)
```
(`search_ids` query builder comes in Task 2.)
- [ ] `.env.example`: append `MEILI_URL=` / `MEILI_KEY=` / `MEILI_INDEX=sightings` with a comment "empty MEILI_URL = search served by SQLite fallback".
- [ ] Run, commit: `feat: meilisearch client — docs, upsert/delete, settings (disabled=no-op)`.

---

### Task 2: `search_ids` query builder + Meili-first read layer

**Files:** Modify `app/search.py`, `app/routes/public.py`; Test `tests/test_search.py`, `tests/test_public.py`.

**Interfaces:**
- `search.search_ids(*, q="", shape=None, country=None, source=None, date_from=None, date_to=None, media_kind=None, has_geo=None, sort="new", top_window="all", page=1, per_page=24, facets=None) -> dict | None` — returns `{"ids": [...], "total": int, "facets": {...}}` or **None** on any failure/disabled (signals fallback). Builds Meili `filter` list (AND of `shape = 'x'`, `country = 'x'`, `source = 'x'`, `media_kind = 'x'`, `has_geo = true`, `sighted_ts >= N`, `sighted_ts <= N`, status filter always `status IN [live, deleted_by_user, removed_on_reddit]`), `sort` (`new`→`sighted_ts:desc`, `old`→`sighted_ts:asc`, `top`→`reddit_score:desc` + window filter), offset/limit, optional `facets`.
- `public.hydrate_cards(conn, ids) -> list[dict]` — fetch rows by id with thumb subselects, order-preserving, `card()`-shaped.
- Gallery `/`, `/api/pins` try Meili first (when enabled), fall back to `query_sightings`.

- [ ] Tests (respx): filter/sort expression assertions (inspect request body: `"shape = 'sphere'"` present, sort `["sighted_ts:desc"]`, top window adds `sighted_ts >=`), None on connect error; gallery route with mocked Meili returns cards in Meili id order (seed 2 rows, mock ids reversed, assert order flipped vs SQL default); `/api/pins` passes `has_geo`.
- [ ] Implement; keep ALL SQL paths untouched as fallback.
- [ ] Run full suite (existing tests = fallback proof), commit: `feat: meili-first gallery + pins with SQL fallback`.

---

### Task 3: /search with facets + hooks at every write point

**Files:** Modify `app/routes/public.py` (search route), `app/templates/search.html`, `app/posting.py`, `app/routes/admin.py`, `ingest.py`, `sync.py`; Test route + hook tests.

**Interfaces:**
- `/search`: params `q, shape, country, source, from, to`; Meili path returns cards + `facets` dict rendered as dropdowns (shape/country/source with counts); FTS5 fallback ignores facets (dropdowns still render from static lists, no counts).
- Hooks: `posting.post_sighting` → `search.index_sightings(conn, [sighting_id])` after commit; admin approve/unhide → index; hide/reject → `delete_sightings`; `ingest_post` → index after commit; `sync_once` → collect changed ids → index at end.

- [ ] Tests: mocked-Meili /search renders facet counts + typo path (just assert q passed through); hook tests assert respx routes called after each action (approve/hide/ingest/sync) with Meili enabled; disabled ⇒ zero network (existing tests).
- [ ] Implement + commit: `feat: faceted /search via meili; index hooks on all write paths`.

---

### Task 4: reindex.py + deploy artifacts + runbook

**Files:** Create `reindex.py`, `deploy/meilisearch.service`; Modify `deploy/RUNBOOK.md`.

- [ ] `reindex.py`: `--wipe` deletes index; `apply_settings()`; walk PUBLIC rows in id batches of 500 → `index_sightings`; prints count. Test: mocked run indexes seeded rows.
- [ ] `deploy/meilisearch.service`:
```ini
[Unit]
Description=Meilisearch for ufosighting.report
After=network.target

[Service]
User=ubuntu
ExecStart=/home/ubuntu/meilisearch --http-addr 127.0.0.1:7700 --db-path /home/ubuntu/ufosighting/data/meili --max-indexing-memory 128Mb --env production --master-key ${MEILI_KEY}
EnvironmentFile=/home/ubuntu/ufosighting/.env
MemoryHigh=250M
MemoryMax=300M
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
- [ ] RUNBOOK: download binary (`curl -L https://install.meilisearch.com | sh`, move to /home/ubuntu), generate MEILI_KEY (`openssl rand -hex 24`), set MEILI_URL=http://127.0.0.1:7700 in .env, install unit, `enable --now`, `python reindex.py`, verify `free -h` headroom + search works, watch `systemctl status meilisearch` for OOM.
- [ ] Full suite green; commit: `feat: reindex script + meilisearch systemd unit + runbook`.

## Post-plan

Deploy steps (after backfill completes): install Meili on VM, keys in .env, deploy code, `reindex.py`, verify RAM + live search, browser pass.
