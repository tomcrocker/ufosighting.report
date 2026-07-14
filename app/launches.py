"""Rocket launches near a sighting's time and place.

Twilight launches (especially Falcon 9 from Vandenberg / the Cape) produce
glowing spiral plumes — the single most-reported "UFO" after Starlink trains.
Launch Library 2 (thespacedevs.com, free) supplies every launch with pad
coordinates; we cache them locally so per-sighting checks are pure math.

A launch is offered as context when the sighting happened between 10 minutes
before liftoff and 60 minutes after (plumes linger), within 1,200 km of the
pad (Vandenberg plumes are reported across the whole US Southwest).
"""
import json
import os
from datetime import datetime, timezone

import httpx

from app.helpers import haversine_km

CACHE = "data/launches.json"
LL2 = "https://ll.thespacedevs.com/2.2.0/launch/"
WINDOW_BEFORE_MIN = 10
WINDOW_AFTER_MIN = 60
MAX_KM = 1200

_cache: list[dict] | None = None


def fetch_range(start_iso: str, end_iso: str) -> int:
    """Fetch launches in [start, end] into the local cache (merged by id).
    LL2 free tier allows 15 req/hr — a whole year is ~3 pages of 100."""
    merged = {l["id"]: l for l in _load_raw()}
    url = LL2
    params = {"net__gte": start_iso, "net__lte": end_iso, "limit": 100,
              "mode": "normal"}
    added = 0
    while url:
        resp = httpx.get(url, params=params, timeout=60,
                         follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        for l in data.get("results", []):
            pad = l.get("pad") or {}
            if not pad.get("latitude"):
                continue
            slim = {
                "id": l["id"],
                "name": l.get("name") or "",
                "provider": (l.get("launch_service_provider") or {}).get("name") or "",
                "net": l.get("net") or "",
                "pad": (pad.get("location") or {}).get("name") or pad.get("name") or "",
                "lat": float(pad["latitude"]),
                "lon": float(pad["longitude"]),
            }
            if slim["id"] not in merged:
                added += 1
            merged[slim["id"]] = slim
        url = data.get("next")
        params = None  # next URL already carries the query string
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(sorted(merged.values(), key=lambda l: l["net"]), f)
    global _cache
    _cache = None
    return added


def _load_raw() -> list[dict]:
    try:
        return json.load(open(CACHE))
    except (FileNotFoundError, ValueError):
        return []


def _launches() -> list[dict]:
    global _cache
    if _cache is None:
        _cache = _load_raw()
    return _cache


def matches(lat: float, lon: float, when_iso: str) -> list[dict]:
    """Launches whose plume could plausibly be this sighting."""
    try:
        when = datetime.strptime(when_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return []
    out = []
    for l in _launches():
        try:
            net = datetime.fromisoformat(l["net"].replace("Z", "+00:00"))
        except ValueError:
            continue
        offset_min = (when - net).total_seconds() / 60
        if not (-WINDOW_BEFORE_MIN <= offset_min <= WINDOW_AFTER_MIN):
            continue
        km = haversine_km(lat, lon, l["lat"], l["lon"])
        if km > MAX_KM:
            continue
        out.append({
            "name": l["name"],
            "provider": l["provider"],
            "pad": l["pad"],
            "distance_km": round(km),
            "minutes_after": round(offset_min),
        })
    return sorted(out, key=lambda m: abs(m["minutes_after"]))[:2]
