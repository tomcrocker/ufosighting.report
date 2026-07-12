import re
import threading
import time

import httpx

from app.config import get_settings

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
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
        or addr.get("hamlet") or addr.get("suburb") or addr.get("municipality") or "",
        "country": addr.get("country", ""),
        "addresstype": item.get("addresstype", ""),
    }


def search(q: str, limit: int = 5) -> list[dict]:
    _throttle()
    resp = httpx.get(
        NOMINATIM_URL,
        params={"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 1,
                "accept-language": "en"},
        headers={"User-Agent": get_settings().user_agent},
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    return [_parse(i) for i in resp.json()]


def _reverse_once(lat: float, lon: float, zoom: int) -> dict | None:
    _throttle()
    try:
        resp = httpx.get(
            REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": zoom,
                    "addressdetails": 1, "accept-language": "en"},
            headers={"User-Agent": get_settings().user_agent},
            timeout=10,
        )
    except httpx.HTTPError:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if resp.status_code != 200 or "error" in data or "lat" not in data:
        return None
    out = _parse(data)
    addr = data.get("address", {})
    out["_region"] = (addr.get("state") or addr.get("province")
                      or addr.get("county") or "")
    return out


def reverse(lat: float, lon: float) -> dict | None:
    """Nearest town/city for a dropped pin. Zoom 10 (city level) first, then
    zoom 12 (village/suburb) when no settlement came back. Returns {label,
    city, country, ...} — never a street address, and never a bare country
    or region: the caller should fall back to coordinates instead, which at
    least are precise (a country-only 'location' is unusable)."""
    out = _reverse_once(lat, lon, 10)
    if out is not None and not out["city"]:
        finer = _reverse_once(lat, lon, 12)
        if finer is not None and finer["city"]:
            out = finer
    if out is None or not out["city"]:
        return None
    parts = [p for p in (out["city"], out.get("_region", ""), out["country"]) if p]
    out["label"] = ", ".join(dict.fromkeys(parts))
    return out


_PARENS = re.compile(r"\s*\([^)]*\)")
_NEAR = re.compile(r"\b(?:near|close to|outside|just outside|west|east|north|south)"
                   r"(?:[- ](?:of|from))?\s+", re.I)


def candidates(location_text: str, city: str | None = None,
               country: str | None = None) -> list[str]:
    """Geocode query ladder for LLM-extracted location strings — Nominatim
    chokes on verbose text ('Jeannette, PA (an hour outside Pittsburgh)'),
    so try progressively simpler variants. Never yields a bare country:
    a country centroid is a useless sighting pin."""
    out: list[str] = []

    def add(q: str | None):
        q = re.sub(r"\s+", " ", (q or "")).strip(" ,")
        if q and q.lower() != (country or "").lower() and q not in out:
            out.append(q)

    text = _PARENS.sub("", location_text or "").strip(" ,")
    add(text)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 2:
        add(", ".join(parts[1:]))          # drop the wordy leading descriptor
        add(", ".join(parts[-2:]))         # the two broadest parts
        # "Region, near Town" style: promote the place after the qualifier
        for p in parts:
            m = _NEAR.search(p)
            if m:
                add(p[m.end():] + ", " + parts[0])
    if city and country:
        add(f"{city}, {country}")
    elif city:
        add(city)
    return out[:5]


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
