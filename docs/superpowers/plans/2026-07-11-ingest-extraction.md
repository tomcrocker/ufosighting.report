# Ingest Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ingested r/UFOs Sighting posts first-class gallery entries by extracting structured date/time/location/shape/duration/object-count via an xAI (Grok) LLM over the combined title+body+OP-comments, geocoding the location to lat/lon for map pins, and deriving a real `sighted_at`.

**Architecture:** A new `app/extract.py` wraps one xAI chat-completions call per post plus a pure `validate_and_clamp()`. A new `app/geocode.py` centralizes Nominatim (throttle + DB cache) and is consumed by both the submit autocomplete endpoint and ingest. `ingest.py` is reworked to orchestrate fetch → OP comments → combine → extract → clamp → geocode → `sighted_at` → insert. Gallery templates gain a "from r/UFOs" badge for `source='reddit'`.

**Tech Stack:** Python 3.12, httpx (xAI + Nominatim), SQLite, zoneinfo, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-07-11-ingest-extraction-design.md`

## Global Constraints

- Extraction provider is **xAI (Grok)**, OpenAI-compatible endpoint `https://api.x.ai/v1/chat/completions`, Bearer `XAI_API_KEY`, model `XAI_MODEL` (default `grok-3-mini`). Empty key ⇒ extraction skipped, ingest still runs best-effort.
- **Best-effort always ingests**: extraction/geocode failure never aborts a post; null lat/lon ⇒ no map pin; no parsed date ⇒ `sighted_at` = post `created_utc`.
- **Do not invent**: the LLM prompt must return null for anything not stated in the text.
- **Validate + clamp in code** (no second LLM call): date parses & 1940≤date≤today; time `HH:MM` 24h; timezone constructs via `zoneinfo`; shape ∈ `helpers.SHAPES`; num_objects ∈ `helpers.NUM_OBJECTS`; duration int 1..86400; strings trimmed + length-capped.
- **Rate discipline**: Nominatim ≥1.1s between calls + DB cache + descriptive UA; Reddit shared script app ⇒ ~2s/post throttle in ingest; xAI is high-limit.
- Ingest source is `INGEST_SUBREDDIT` (default = `SUBREDDIT`); submissions still post to `SUBREDDIT`.
- Reuse existing modules/patterns; only `source='reddit'` distinguishes ingested rows (no new sightings columns). New table `geocode_cache` via idempotent `init_db`.
- Run tests: `.venv/bin/pytest -q` from repo root. Commit at the end of every task.

## File Structure

```
app/
  config.py     # + xai_api_key, xai_model, ingest_subreddit
  db.py         # + geocode_cache table (in SCHEMA_TABLES)
  extract.py    # NEW combine_post_text, extract_fields (xAI), validate_and_clamp
  geocode.py    # NEW search(), forward(), throttle, DB cache; used by submit + ingest
  routes/
    submit.py   # /api/geocode becomes thin wrapper over geocode.search()
  templates/
    _cards.html # + "from r/UFOs" badge when source=='reddit'
    detail.html # + auto-extracted note when source=='reddit'
ingest.py       # reworked: OP comments, extract, geocode, build_sighted_at, richer insert
tests/
  test_extract.py, test_geocode.py, test_ingest.py (rewrite), test_submit.py (geocode wrapper)
.env.example    # + XAI_*, INGEST_SUBREDDIT
```

---

### Task 1: Config (xAI + ingest subreddit) + geocode_cache table

**Files:**
- Modify: `app/config.py`, `app/db.py`, `.env.example`
- Test: `tests/test_config.py`, `tests/test_db.py`

**Interfaces:**
- Produces (`Settings`): `xai_api_key: str`, `xai_model: str` (default `grok-3-mini`), `ingest_subreddit: str` (default = `subreddit` when `INGEST_SUBREDDIT` unset).
- Produces (schema): table `geocode_cache(query TEXT PRIMARY KEY, lat REAL, lon REAL, city TEXT, country TEXT, display_name TEXT, cached_at TEXT DEFAULT now)`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:
```python
def test_extraction_settings_defaults(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)
    monkeypatch.delenv("INGEST_SUBREDDIT", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.xai_api_key == ""
    assert s.xai_model == "grok-3-mini"
    assert s.ingest_subreddit == s.subreddit   # falls back to SUBREDDIT


def test_ingest_subreddit_override(monkeypatch):
    monkeypatch.setenv("INGEST_SUBREDDIT", "UFOs")
    get_settings.cache_clear()
    assert get_settings().ingest_subreddit == "UFOs"
```

Add to `tests/test_db.py`:
```python
def test_geocode_cache_table(db_conn):
    db_conn.execute("INSERT INTO geocode_cache (query, lat, lon, city, country, display_name) "
                    "VALUES ('tofino bc', 49.15, -125.9, 'Tofino', 'Canada', 'Tofino, BC, Canada')")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM geocode_cache WHERE query='tofino bc'").fetchone()
    assert row["lat"] == 49.15 and row["city"] == "Tofino"
```

- [ ] **Step 2: Run — expect fail**

`.venv/bin/pytest tests/test_config.py::test_extraction_settings_defaults tests/test_db.py::test_geocode_cache_table -q` → FAIL.

- [ ] **Step 3: Implement config**

In `app/config.py` `Settings` (after `verify_dm_per_username_hours`):
```python
    xai_api_key: str
    xai_model: str
    ingest_subreddit: str
```
In `get_settings()`, before the `return Settings(`:
```python
    subreddit = _env("SUBREDDIT")
```
Change `subreddit=_env("SUBREDDIT"),` to `subreddit=subreddit,` and append after `verify_dm_per_username_hours=...`:
```python
        xai_api_key=_env("XAI_API_KEY", ""),
        xai_model=_env("XAI_MODEL", "grok-3-mini"),
        ingest_subreddit=_env("INGEST_SUBREDDIT", "") or subreddit,
```

- [ ] **Step 4: Implement schema**

In `app/db.py` `SCHEMA_TABLES`, after the `rate_events` table:
```sql
CREATE TABLE IF NOT EXISTS geocode_cache (
  query TEXT PRIMARY KEY,
  lat REAL,
  lon REAL,
  city TEXT,
  country TEXT,
  display_name TEXT,
  cached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

- [ ] **Step 5: `.env.example`** — append:
```bash
# xAI (Grok) — ingest extraction of date/time/location. Empty = skip extraction.
XAI_API_KEY=
XAI_MODEL=grok-3-mini
# Ingest source subreddit (pull Sighting posts FROM here). Defaults to SUBREDDIT.
INGEST_SUBREDDIT=UFOs
```

- [ ] **Step 6: Run — expect pass**; then commit.
```bash
git add app/config.py app/db.py .env.example tests/
git commit -m "feat: xAI + ingest-subreddit config, geocode_cache table"
```

---

### Task 2: `app/extract.py` — combine + validate/clamp (no network yet)

**Files:**
- Create: `app/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces: `combine_post_text(post: dict, op_comments: list[str]) -> str`; `validate_and_clamp(raw: dict, *, post_created_iso: str) -> dict`. The clamped dict has keys: `date` (YYYY-MM-DD|None), `time` (HH:MM|None), `timezone` (str|None), `location_text` (str|None), `city` (str|None), `country` (str|None), `shape` (str|None), `num_objects` (str|None), `duration_seconds` (int|None), `summary` (str|None).
- Consumes: `helpers.SHAPES`, `helpers.NUM_OBJECTS`.

- [ ] **Step 1: Failing tests** — `tests/test_extract.py`:
```python
from app import extract


def test_combine_labels_sources():
    post = {"title": "Orb over Tofino", "selftext": "Saw it at dusk."}
    text = extract.combine_post_text(post, ["It was near the pier", "About 9pm"])
    assert "Orb over Tofino" in text and "Saw it at dusk." in text
    assert "near the pier" in text and "About 9pm" in text
    assert "TITLE" in text and "OP COMMENT" in text


def test_combine_truncates(monkeypatch):
    post = {"title": "t", "selftext": "x" * 20000}
    text = extract.combine_post_text(post, [])
    assert len(text) <= 6500  # capped


def test_clamp_keeps_valid():
    raw = {"date": "2026-07-01", "time": "22:15", "timezone": "America/Vancouver",
           "location_text": "Lake Cowichan, BC", "city": "Lake Cowichan", "country": "Canada",
           "shape": "Sphere", "num_objects": "2", "duration_seconds": 120, "summary": "An orb."}
    c = extract.validate_and_clamp(raw, post_created_iso="2026-07-05T00:00:00Z")
    assert c["date"] == "2026-07-01" and c["time"] == "22:15"
    assert c["timezone"] == "America/Vancouver"
    assert c["shape"] == "sphere" and c["num_objects"] == "2"
    assert c["duration_seconds"] == 120 and c["city"] == "Lake Cowichan"


def test_clamp_drops_future_and_ancient_dates():
    assert extract.validate_and_clamp({"date": "2999-01-01"}, post_created_iso="2026-07-05T00:00:00Z")["date"] is None
    assert extract.validate_and_clamp({"date": "1800-01-01"}, post_created_iso="2026-07-05T00:00:00Z")["date"] is None


def test_clamp_drops_bad_values():
    raw = {"time": "9pm", "timezone": "Mars/Olympus", "shape": "mothership",
           "num_objects": "lots", "duration_seconds": 999999}
    c = extract.validate_and_clamp(raw, post_created_iso="2026-07-05T00:00:00Z")
    assert c["time"] is None and c["timezone"] is None and c["shape"] is None
    assert c["num_objects"] is None and c["duration_seconds"] is None


def test_clamp_handles_empty():
    c = extract.validate_and_clamp({}, post_created_iso="2026-07-05T00:00:00Z")
    assert all(c[k] is None for k in ("date", "time", "location_text", "shape"))
```

- [ ] **Step 2: Run — expect fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `app/extract.py`** (combine + clamp; `extract_fields` added in Task 3):
```python
import re
from datetime import date as _date, datetime, timezone

from app import helpers

MAX_SOURCE = 2000
MAX_TOTAL = 6500
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def combine_post_text(post: dict, op_comments: list[str]) -> str:
    parts = [f"[TITLE] {(post.get('title') or '')[:MAX_SOURCE]}"]
    body = (post.get("selftext") or "").strip()
    if body:
        parts.append(f"[BODY] {body[:MAX_SOURCE]}")
    for c in op_comments:
        c = (c or "").strip()
        if c:
            parts.append(f"[OP COMMENT] {c[:MAX_SOURCE]}")
    return "\n".join(parts)[:MAX_TOTAL]


def _clean_str(v, cap):
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:cap] if v else None


def validate_and_clamp(raw: dict, *, post_created_iso: str) -> dict:
    out = {k: None for k in (
        "date", "time", "timezone", "location_text", "city", "country",
        "shape", "num_objects", "duration_seconds", "summary")}
    if not isinstance(raw, dict):
        return out

    d = raw.get("date")
    if isinstance(d, str) and _DATE_RE.match(d.strip()):
        try:
            parsed = datetime.strptime(d.strip(), "%Y-%m-%d").date()
            if _date(1940, 1, 1) <= parsed <= datetime.now(timezone.utc).date():
                out["date"] = d.strip()
        except ValueError:
            pass

    t = raw.get("time")
    if isinstance(t, str) and _TIME_RE.match(t.strip()):
        hh, mm = t.strip().split(":")
        out["time"] = f"{int(hh):02d}:{mm}"

    tz = raw.get("timezone")
    if isinstance(tz, str) and tz.strip():
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz.strip())
            out["timezone"] = tz.strip()
        except Exception:
            pass

    out["location_text"] = _clean_str(raw.get("location_text"), 300)
    out["city"] = _clean_str(raw.get("city"), 120)
    out["country"] = _clean_str(raw.get("country"), 120)
    out["summary"] = _clean_str(raw.get("summary"), 500)

    shape = raw.get("shape")
    if isinstance(shape, str) and shape.strip().lower() in helpers.SHAPES:
        out["shape"] = shape.strip().lower()

    n = raw.get("num_objects")
    if isinstance(n, str) and n.strip() in helpers.NUM_OBJECTS:
        out["num_objects"] = n.strip()
    elif isinstance(n, int) and str(n) in helpers.NUM_OBJECTS:
        out["num_objects"] = str(n)

    dur = raw.get("duration_seconds")
    if isinstance(dur, bool):
        dur = None
    if isinstance(dur, (int, float)) and 1 <= int(dur) <= 86400:
        out["duration_seconds"] = int(dur)

    return out
```

- [ ] **Step 4: Run — expect pass**; commit.
```bash
git add app/extract.py tests/test_extract.py
git commit -m "feat: post-text combiner + validate/clamp for LLM extraction"
```

---

### Task 3: `extract_fields` — xAI call

**Files:**
- Modify: `app/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces: `extract_fields(text: str) -> dict` — POSTs to `https://api.x.ai/v1/chat/completions` with `XAI_MODEL`, a JSON-only system prompt, `response_format={"type":"json_object"}`; returns the parsed dict, or `{}` on empty key / network error / non-JSON / missing content.
- Consumes: `get_settings().xai_api_key`, `.xai_model`.

- [ ] **Step 1: Failing tests** — add to `tests/test_extract.py`:
```python
import httpx, respx
from app.config import get_settings

CHAT = "https://api.x.ai/v1/chat/completions"


def _chat_response(content: str):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_extract_fields_empty_key_returns_empty(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "")
    get_settings.cache_clear()
    assert extract.extract_fields("anything") == {}


@respx.mock
def test_extract_fields_parses_json(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    get_settings.cache_clear()
    route = respx.post(CHAT).mock(return_value=_chat_response(
        '{"date":"2026-07-01","location_text":"Tofino, BC","shape":"sphere"}'))
    out = extract.extract_fields("Orb over Tofino on 2026-07-01")
    assert out["date"] == "2026-07-01" and out["location_text"] == "Tofino, BC"
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer xai-test"


@respx.mock
def test_extract_fields_non_json_returns_empty(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    get_settings.cache_clear()
    respx.post(CHAT).mock(return_value=_chat_response("sorry, I cannot help"))
    assert extract.extract_fields("x") == {}


@respx.mock
def test_extract_fields_network_error_returns_empty(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    get_settings.cache_clear()
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("down"))
    assert extract.extract_fields("x") == {}
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement** — add to `app/extract.py`:
```python
import json

import httpx

from app.config import get_settings

CHAT_URL = "https://api.x.ai/v1/chat/completions"

SYSTEM_PROMPT = (
    "You extract structured UFO-sighting metadata from a Reddit post. "
    "Return ONLY a JSON object with keys: date (YYYY-MM-DD), time (HH:MM 24h), "
    "timezone (IANA name like America/Vancouver), location_text, city, country, "
    "shape, num_objects (one of 1,2,3,4,5+), duration_seconds (integer), "
    "summary (one neutral sentence). Use null for any field NOT explicitly "
    "stated in the text — do NOT guess or invent. shape must be one of: "
    + ", ".join(helpers.SHAPES) + "."
)


def extract_fields(text: str) -> dict:
    s = get_settings()
    if not s.xai_api_key:
        return {}
    try:
        resp = httpx.post(
            CHAT_URL,
            headers={"Authorization": f"Bearer {s.xai_api_key}"},
            json={
                "model": s.xai_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=45,
        )
        if resp.status_code != 200:
            return {}
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return {}
```

- [ ] **Step 4: Run — expect pass**; commit.
```bash
git add app/extract.py tests/test_extract.py
git commit -m "feat: xAI chat-completions extraction call (json_object, graceful fallback)"
```

---

### Task 4: `app/geocode.py` — shared geocoder + DB cache, refactor submit

**Files:**
- Create: `app/geocode.py`
- Modify: `app/routes/submit.py` (delegate `/api/geocode` + drop inline Nominatim), `tests/test_submit.py` (patch target)
- Test: `tests/test_geocode.py`

**Interfaces:**
- Produces: `search(q: str, limit: int = 5) -> list[dict]` (each `{display_name, lat, lon, city, country}`); `forward(conn, q: str) -> dict | None` (single best `{lat, lon, city, country, display_name}`, DB-cached in `geocode_cache`); module `_throttle()` enforcing ≥1.1s between real Nominatim calls; `NOMINATIM_URL`.
- Consumes: `get_settings().user_agent`.

- [ ] **Step 1: Failing tests** — `tests/test_geocode.py`:
```python
import httpx, respx
from app import geocode

NOM = "https://nominatim.openstreetmap.org/search"


def _hit():
    return httpx.Response(200, json=[{
        "display_name": "Tofino, BC, Canada", "lat": "49.153", "lon": "-125.905",
        "address": {"town": "Tofino", "country": "Canada"}}])


@respx.mock
def test_search_parses(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get(NOM).mock(return_value=_hit())
    out = geocode.search("Tofino")
    assert out[0]["city"] == "Tofino" and out[0]["country"] == "Canada"
    assert abs(out[0]["lat"] - 49.153) < 1e-6


@respx.mock
def test_forward_best_match_and_caches(db_conn, monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    route = respx.get(NOM).mock(return_value=_hit())
    r1 = geocode.forward(db_conn, "Tofino BC")
    assert r1["lat"] and r1["city"] == "Tofino"
    # cached: second call must NOT hit the network
    r2 = geocode.forward(db_conn, "Tofino BC")
    assert r2["city"] == "Tofino"
    assert route.call_count == 1


@respx.mock
def test_forward_no_result_returns_none(db_conn, monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get(NOM).mock(return_value=httpx.Response(200, json=[]))
    assert geocode.forward(db_conn, "asdfqwer nowhere") is None
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `app/geocode.py`**:
```python
import threading
import time

import httpx

from app.config import get_settings

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_MIN_INTERVAL = 1.1
_lock = threading.Lock()
_last_call = [0.0]


def _throttle():
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _parse(item: dict) -> dict:
    addr = item.get("address", {})
    return {
        "display_name": item.get("display_name", ""),
        "lat": float(item["lat"]),
        "lon": float(item["lon"]),
        "city": addr.get("city") or addr.get("town") or addr.get("village")
        or addr.get("municipality") or "",
        "country": addr.get("country", ""),
    }


def search(q: str, limit: int = 5) -> list[dict]:
    _throttle()
    resp = httpx.get(
        NOMINATIM_URL,
        params={"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 1},
        headers={"User-Agent": get_settings().user_agent},
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    return [_parse(i) for i in resp.json()]


def forward(conn, q: str) -> dict | None:
    key = q.strip().lower()
    if not key:
        return None
    row = conn.execute(
        "SELECT lat, lon, city, country, display_name FROM geocode_cache WHERE query=?",
        (key,),
    ).fetchone()
    if row is not None:
        return None if row["lat"] is None else dict(row)
    results = search(q, limit=1)
    best = results[0] if results else None
    conn.execute(
        """INSERT OR REPLACE INTO geocode_cache (query, lat, lon, city, country, display_name)
           VALUES (?,?,?,?,?,?)""",
        (key, best["lat"] if best else None, best["lon"] if best else None,
         best["city"] if best else None, best["country"] if best else None,
         best["display_name"] if best else None),
    )
    conn.commit()
    return best
```

- [ ] **Step 4: Refactor `app/routes/submit.py`** — replace the inline Nominatim block in the `geocode` route with a call to `geocode.search()`. Change the import line `from app import db, helpers, r2, ratelimit, reddit, turnstile, verify` to also import `geocode`. Replace the body after the rate-limit check + cache lookup:
```python
    ratelimit.record(conn, ip, "geocode")
    try:
        results = geocode.search(q, limit=5)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Geocoder unavailable, drop a pin instead")
    if len(_geocode_cache) < 5000:
        _geocode_cache[cache_key] = results
    return {"results": results}
```
(Keep the existing `_geocode_cache` in-memory map and the `GEOCODE_URL` constant can be removed; `httpx` import stays.)

- [ ] **Step 5: Fix `tests/test_submit.py`** — the geocode test there monkeypatches nothing network-y (short query returns early), so it still passes. No change needed unless a test patched `app.routes.submit.httpx` for geocode (it doesn't). Run the file to confirm.

- [ ] **Step 6: Run — expect pass** (`tests/test_geocode.py tests/test_submit.py`); commit.
```bash
git add app/geocode.py app/routes/submit.py tests/test_geocode.py
git commit -m "feat: shared Nominatim geocoder with throttle + DB cache; submit delegates to it"
```

---

### Task 5: Rework `ingest.py` — orchestrate extraction + geocode + sighted_at

**Files:**
- Modify: `ingest.py`
- Test: `tests/test_ingest.py` (rewrite)

**Interfaces:**
- Consumes: `extract.combine_post_text/extract_fields/validate_and_clamp`, `geocode.forward`, `reddit.script_token/list_flair_posts`, `r2.put_bytes`, `helpers`, `get_settings().ingest_subreddit`.
- Produces: `fetch_op_comments(token, post) -> list[str]`; `build_sighted_at(clamped: dict, post_created_iso: str) -> tuple[str, str]` (returns `(sighted_at_utc_iso, tz_name)`); reworked `ingest_post(conn, post, token=None) -> bool`; `ingest_once`/`main` use `ingest_subreddit`.

- [ ] **Step 1: Rewrite tests** — `tests/test_ingest.py`:
```python
import ingest


def _post(pid="p1", **over):
    d = {"id": pid, "title": "Orb over Tofino", "author": "witness9",
         "selftext": "Saw an amber orb at dusk near the inlet.", "created_utc": 1751000000,
         "permalink": f"/r/UFOs/comments/{pid}/x/", "url": "https://reddit.com/x",
         "link_flair_text": "Sighting", "is_self": True}
    d.update(over)
    return d


def _stub_pipeline(monkeypatch, *, clamped=None, coords=None, comments=None):
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    monkeypatch.setattr(ingest, "fetch_op_comments", lambda token, post: comments or [])
    monkeypatch.setattr(ingest.extract, "extract_fields", lambda text: {})
    monkeypatch.setattr(ingest.extract, "validate_and_clamp",
                        lambda raw, post_created_iso: clamped or _empty_clamped())
    monkeypatch.setattr(ingest.geocode, "forward", lambda conn, q: coords)


def _empty_clamped():
    return {k: None for k in ("date", "time", "timezone", "location_text", "city",
                              "country", "shape", "num_objects", "duration_seconds", "summary")}


def test_build_sighted_at_with_date_time_tz():
    c = _empty_clamped(); c.update(date="2026-07-01", time="22:15", timezone="America/Vancouver")
    iso, tz = ingest.build_sighted_at(c, "2026-07-05T00:00:00Z")
    assert iso == "2026-07-02T05:15:00Z" and tz == "America/Vancouver"


def test_build_sighted_at_no_time_uses_noon():
    c = _empty_clamped(); c.update(date="2026-07-01", timezone="America/Vancouver")
    iso, tz = ingest.build_sighted_at(c, "2026-07-05T00:00:00Z")
    assert iso == "2026-07-01T19:00:00Z"  # noon PDT = 19:00 UTC


def test_build_sighted_at_no_date_uses_post_time():
    iso, tz = ingest.build_sighted_at(_empty_clamped(), "2026-07-05T00:00:00Z")
    assert iso == "2026-07-05T00:00:00Z" and tz == "UTC"


def test_ingest_post_populates_extracted_fields(db_conn, monkeypatch):
    clamped = _empty_clamped()
    clamped.update(date="2026-07-01", time="22:15", timezone="America/Vancouver",
                   location_text="Tofino, BC", shape="sphere", num_objects="2",
                   duration_seconds=120)
    _stub_pipeline(monkeypatch, clamped=clamped,
                   coords={"lat": 49.15, "lon": -125.9, "city": "Tofino", "country": "Canada",
                           "display_name": "Tofino, BC, Canada"})
    assert ingest.ingest_post(db_conn, _post(), token="t") is True
    row = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='p1'").fetchone()
    assert row["source"] == "reddit" and row["status"] == "live"
    assert row["reddit_username"] == "witness9"
    assert row["shape"] == "sphere" and row["num_objects"] == "2"
    assert row["lat"] == 49.15 and row["country"] == "Canada"
    assert row["sighted_at"] == "2026-07-02T05:15:00Z"


def test_ingest_post_best_effort_when_empty(db_conn, monkeypatch):
    _stub_pipeline(monkeypatch)  # empty clamp, no coords
    assert ingest.ingest_post(db_conn, _post(), token="t") is True
    row = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='p1'").fetchone()
    assert row["lat"] is None and row["shape"] is None
    # sighted_at falls back to post created time
    assert row["sighted_at"].startswith("2025-")  # created_utc 1751000000 -> 2025


def test_ingest_dedup(db_conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    ingest.ingest_post(db_conn, _post(), token="t")
    assert ingest.ingest_post(db_conn, _post(), token="t") is False
    assert db_conn.execute("SELECT COUNT(*) FROM sightings WHERE reddit_post_id='p1'").fetchone()[0] == 1


def test_ingest_once_uses_ingest_subreddit(db_conn, monkeypatch):
    seen = {}
    monkeypatch.setattr(ingest.reddit, "script_token", lambda: "t")
    def fake_list(tok, **k):
        seen.update(k)
        return ([_post("a")], None)
    monkeypatch.setattr(ingest.reddit, "list_flair_posts", fake_list)
    _stub_pipeline(monkeypatch)
    ingest.ingest_once(db_conn)
    # INGEST_SUBREDDIT unset in tests ⇒ falls back to SUBREDDIT (UFOs_sandbox from conftest)
    assert seen["subreddit"] == "UFOs_sandbox"
    assert seen["flair"] == "Sighting"
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Rewrite `ingest.py`** orchestration:
```python
"""Ingest Sighting-flaired posts from the subreddit into the gallery, extracting
date/time/location via LLM + geocoding. Run by ufosighting-ingest.timer;
`--backfill` walks the last 30 days once."""
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

from app import db, extract, geocode, helpers, r2, reddit
from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
CT_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
BACKFILL_PAGE_SLEEP_SECONDS = 3
PER_POST_SLEEP_SECONDS = 2
BACKFILL_DAYS = 30


def _fetch_image(url: str):
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"User-Agent": get_settings().user_agent})
    if resp.status_code != 200:
        return None
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    return resp.content, ct, CT_EXT.get(ct, ".jpg")


def download_media(post: dict) -> list[tuple[bytes, str, str]]:
    out = []
    url = post.get("url", "") or ""
    gallery = post.get("media_metadata")
    if gallery:
        for item in gallery.values():
            src = (item.get("s", {}) or {}).get("u")
            if item.get("e") == "Image" and src:
                out.append(_fetch_image(src.replace("&amp;", "&")))
    elif url.lower().endswith(IMAGE_EXTS) or "i.redd.it" in url:
        out.append(_fetch_image(url))
    return [m for m in out if m]


def fetch_op_comments(token, post) -> list[str]:
    author = post.get("author")
    if not token or not author:
        return []
    try:
        resp = httpx.get(
            f"https://oauth.reddit.com/comments/{post['id']}",
            params={"depth": 1, "limit": 30, "sort": "top"},
            headers={"Authorization": f"bearer {token}", "User-Agent": get_settings().user_agent},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        listing = resp.json()
        if len(listing) < 2:
            return []
        out = []
        for child in listing[1]["data"]["children"]:
            d = child.get("data", {})
            if d.get("author") == author and d.get("body"):
                out.append(d["body"])
        return out[:10]
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []


def build_sighted_at(clamped: dict, post_created_iso: str) -> tuple[str, str]:
    if not clamped.get("date"):
        return post_created_iso, "UTC"
    tz_name = clamped.get("timezone") or "UTC"
    tm = clamped.get("time") or "12:00"
    try:
        return helpers.to_utc(clamped["date"], tm, tz_name).strftime(ISO), tz_name
    except Exception:
        return post_created_iso, "UTC"


def ingest_post(conn, post: dict, token=None) -> bool:
    pid = post["id"]
    if conn.execute("SELECT 1 FROM sightings WHERE reddit_post_id=?", (pid,)).fetchone():
        return False
    post_created_iso = datetime.fromtimestamp(post.get("created_utc", 0), timezone.utc).strftime(ISO)

    op_comments = fetch_op_comments(token, post)
    text = extract.combine_post_text(post, op_comments)
    clamped = extract.validate_and_clamp(extract.extract_fields(text),
                                         post_created_iso=post_created_iso)

    coords = None
    if clamped.get("location_text"):
        coords = geocode.forward(conn, clamped["location_text"])

    sighted_at, tz_name = build_sighted_at(clamped, post_created_iso)
    title = (post.get("title") or "Untitled sighting")[:300]
    description = (post.get("selftext") or "").strip() or (clamped.get("summary") or "")
    city = (coords or {}).get("city") or clamped.get("city")
    country = (coords or {}).get("country") or clamped.get("country")

    cur = conn.execute(
        """INSERT INTO sightings
             (source, reddit_username, title, description, sighted_at, tz_name,
              shape, num_objects, duration_seconds, location_text, city, country,
              lat, lon, reddit_post_id, status)
           VALUES ('reddit',?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'live')""",
        (post.get("author") or "unknown", title, description, sighted_at, tz_name,
         clamped.get("shape"), clamped.get("num_objects"), clamped.get("duration_seconds"),
         clamped.get("location_text"), city, country,
         (coords or {}).get("lat"), (coords or {}).get("lon"), pid),
    )
    sid = cur.lastrowid
    conn.commit()
    try:
        for i, (data, ct, ext) in enumerate(download_media(post)):
            now = datetime.now(timezone.utc)
            key = f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"
            r2.put_bytes(key, data, ct)
            conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) "
                         "VALUES (?,?, 'image', ?)", (sid, key, i))
        conn.commit()
    except Exception as exc:
        print(f"ingest media for {pid} failed: {exc}")
    return True


def ingest_once(conn, *, limit=100, after=None) -> dict:
    s = get_settings()
    token = reddit.script_token()
    posts, _after = reddit.list_flair_posts(token, subreddit=s.ingest_subreddit,
                                            flair="Sighting", limit=limit, after=after)
    added = 0
    for p in posts:
        if ingest_post(conn, p, token=token):
            added += 1
            time.sleep(PER_POST_SLEEP_SECONDS)
    return {"seen": len(posts), "added": added}


def main(backfill: bool = False) -> None:
    conn = db.connect(get_settings().db_path)
    try:
        if backfill:
            cutoff = time.time() - BACKFILL_DAYS * 86400
            after, total, stop = None, 0, False
            while not stop:
                s = get_settings()
                token = reddit.script_token()
                posts, after = reddit.list_flair_posts(token, subreddit=s.ingest_subreddit,
                                                       flair="Sighting", limit=100, after=after)
                if not posts:
                    break
                for p in posts:
                    if p.get("created_utc", 0) < cutoff:
                        stop = True
                        break
                    if ingest_post(conn, p, token=token):
                        total += 1
                        time.sleep(PER_POST_SLEEP_SECONDS)
                if not after:
                    break
                time.sleep(BACKFILL_PAGE_SLEEP_SECONDS)
            print(f"ingest backfill: added={total}")
        else:
            print("ingest:", ingest_once(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main(backfill="--backfill" in sys.argv)
```

- [ ] **Step 4: Run — expect pass** (`tests/test_ingest.py`); commit.
```bash
git add ingest.py tests/test_ingest.py
git commit -m "feat: ingest extracts date/time/location via LLM + geocode, builds sighted_at"
```

---

### Task 6: Gallery "from r/UFOs" badge

**Files:**
- Modify: `app/templates/_cards.html`, `app/templates/detail.html`, `static/css/site.css`
- Test: `tests/test_public.py`

**Interfaces:**
- Consumes: `source` column already selected by `query_sightings` (`SELECT s.*`) and present in `detail`'s `s` dict.

- [ ] **Step 1: Failing tests** — add to `tests/test_public.py`:
```python
def test_reddit_source_shows_badge(client, app_db):
    seed(app_db, title="Ingested sighting", source="reddit", reddit_post_id="zz1")
    r = client.get("/")
    assert "from r/UFOs" in r.text


def test_site_source_no_badge(client, app_db):
    seed(app_db, title="Site sighting", source="site")
    r = client.get("/")
    assert "from r/UFOs" not in r.text


def test_detail_reddit_note(client, app_db):
    sid = seed(app_db, title="Ingested detail", source="reddit", reddit_post_id="zz2")
    r = client.get(f"/sighting/{sid}")
    assert "auto-extracted" in r.text.lower()
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Badge in `_cards.html`** — inside the `.meta` block, after the `<p class="sub">` line:
```html
      {% if c.source == 'reddit' %}<p class="src-badge">from r/UFOs</p>{% endif %}
```
(`card()` returns `dict(row)` so `c.source` is available.)

- [ ] **Step 4: Note in `detail.html`** — after the `<h1>{{ s.title }}</h1>` line in the viewer column:
```html
    {% if s.source == 'reddit' %}
    <p class="muted src-note">Details auto-extracted from the original
      <a href="{{ reddit_url }}">Reddit post</a>.</p>
    {% endif %}
```

- [ ] **Step 5: CSS** — append to `static/css/site.css`:
```css
.src-badge { display: inline-block; margin-top: 6px; font-size: .72rem; letter-spacing: .04em;
  color: var(--accent2); border: 1px solid var(--line); border-radius: 6px; padding: 1px 7px; }
.src-note { font-size: .85rem; margin-top: 4px; }
```

- [ ] **Step 6: Run — expect pass**; full suite; commit.
```bash
git add app/templates/_cards.html app/templates/detail.html static/css/site.css tests/test_public.py
git commit -m "feat: 'from r/UFOs' badge + auto-extracted note for ingested sightings"
```

---

### Task 7: Full suite gate + dry-run harness doc

**Files:**
- Modify: `deploy/RUNBOOK.md`
- Test: full `.venv/bin/pytest -q` green

- [ ] **Step 1: Full suite** — `.venv/bin/pytest -q` → all green. Fix stragglers.

- [ ] **Step 2: RUNBOOK** — add an "Ingest extraction" subsection under the pivot section:
```markdown
### Ingest extraction (xAI + geocode)
- [ ] Add to VM .env: XAI_API_KEY (xai-...), XAI_MODEL=grok-3-mini, INGEST_SUBREDDIT=UFOs
- [ ] Deploy (geocode_cache table auto-created by init_db)
- [ ] Dry-run small first: temporarily set BACKFILL_DAYS=2 in ingest.py (or just run
      `ingest_once`) and eyeball a few extracted rows in /admin + the gallery
- [ ] Full 30-day backfill: `cd /home/ubuntu/ufosighting && set -a; . .env; set +a;
      .venv/bin/python ingest.py --backfill`  (throttled ~2s/post — expect minutes)
- [ ] Spot-check map pins + sighting dates
- [ ] Enable ongoing ingest: `sudo systemctl enable --now ufosighting-ingest.timer`
```

- [ ] **Step 3: Commit.**
```bash
git add deploy/RUNBOOK.md
git commit -m "docs: ingest extraction rollout steps in runbook"
```

---

## Post-plan notes

- **Deploy is separate** (not a plan task): set `XAI_API_KEY` + `INGEST_SUBREDDIT=UFOs` on the VM, `bash deploy/deploy.sh`, dry-run small, then 30-day backfill, then enable the ingest timer.
- The extractor is provider-agnostic — swap base URL/model/key to change LLMs.
- Movement / distance / apparent-size extraction is intentionally out of scope; the schema already holds them for site submissions and the prompt can be extended later.
