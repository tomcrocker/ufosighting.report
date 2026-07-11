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
        or addr.get("municipality") or "",
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


def reverse(lat: float, lon: float) -> dict | None:
    """Nearest town/city for a dropped pin (Nominatim zoom 10 = city level).
    Returns {label, city, country, ...} with a short human label — never a
    street address — or None if the geocoder is unavailable."""
    _throttle()
    try:
        resp = httpx.get(
            REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
                    "addressdetails": 1, "accept-language": "en"},
            headers={"User-Agent": get_settings().user_agent},
            timeout=10,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or "error" in resp.json():
        return None
    out = _parse(resp.json())
    addr = resp.json().get("address", {})
    region = addr.get("state") or addr.get("province") or addr.get("county") or ""
    parts = [p for p in (out["city"], region, out["country"]) if p]
    out["label"] = ", ".join(dict.fromkeys(parts)) or out["display_name"]
    return out


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
