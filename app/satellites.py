"""Which satellites were actually overhead at a sighting's time and place?

Nothing public offers historical satellite passes (CalSky died in 2020), so
we compute them: CelesTrak TLEs + SGP4 via skyfield. Catalogs are cached
daily under data/tle/ — an archive that makes past dates checkable as the
site ages. Current-epoch TLEs back-propagate usefully for ~2 weeks; beyond
the nearest cached catalog we say so instead of guessing (Phase 2 =
Space-Track historical elements).

Visibility = satellite above the horizon AND sunlit AND observer in
twilight/darkness — daytime or earth-shadow passes aren't what witnesses see.
"""
import os
from datetime import datetime, timedelta, timezone

import httpx

TLE_DIR = "data/tle"
GROUPS = ("stations", "visual", "starlink")
GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle"
MAX_TLE_AGE_DAYS = 14
WINDOW_MIN = 15          # look ±15 min around the sighting
BRIGHT_MIN_ALT = 15      # degrees
STARLINK_MIN_ALT = 20
TRAIN_MIN_SATS = 5       # same launch batch, sunlit, overhead = a train

_loader = None
_eph = None


def _sf():
    global _loader
    if _loader is None:
        from skyfield.api import Loader
        os.makedirs(TLE_DIR, exist_ok=True)
        _loader = Loader("data", verbose=False)
    return _loader


def _ephemeris():
    """Sun ephemeris for sunlit/twilight tests; downloaded once (~17 MB).
    Returns None when unavailable (visibility then can't be filtered)."""
    global _eph
    if _eph is None:
        try:
            _eph = _sf()("de421.bsp")
        except Exception:
            return None
    return _eph


def fetch_today(groups=GROUPS) -> list[str]:
    """Cache today's catalogs; called by the worker once per day."""
    os.makedirs(TLE_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    written = []
    for g in groups:
        path = os.path.join(TLE_DIR, f"{g}-{today}.tle")
        if os.path.exists(path):
            continue
        resp = httpx.get(GP_URL.format(g), timeout=60, follow_redirects=True)
        if resp.status_code == 200 and "1 " in resp.text[:200]:
            with open(path, "w") as f:
                f.write(resp.text)
            written.append(path)
    return written


def _nearest_catalog_date(day: str) -> str | None:
    """Cached catalog date closest to `day`, within MAX_TLE_AGE_DAYS."""
    try:
        dates = sorted({f.split("-", 1)[1].removesuffix(".tle")
                        for f in os.listdir(TLE_DIR) if f.endswith(".tle")})
    except FileNotFoundError:
        return None
    if not dates:
        return None
    want = datetime.strptime(day, "%Y-%m-%d")
    best = min(dates, key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d") - want).days))
    if abs((datetime.strptime(best, "%Y-%m-%d") - want).days) > MAX_TLE_AGE_DAYS:
        return None
    return best


def _load_sats(catalog_date: str):
    from skyfield.api import load
    sats = []
    for g in GROUPS:
        path = os.path.join(TLE_DIR, f"{g}-{catalog_date}.tle")
        if os.path.exists(path):
            sats.extend((g, s) for s in load.tle_file(path))
    # dedup by NORAD id (visual overlaps stations)
    seen = set()
    out = []
    for g, s in sats:
        if s.model.satnum in seen:
            continue
        seen.add(s.model.satnum)
        out.append((g, s))
    return out


def _compass(az_deg: float) -> str:
    from app.helpers import compass_name
    return compass_name(az_deg)


def passes_for(lat: float, lon: float, when_iso: str) -> dict:
    """Compute overhead satellites for a sighting. Returns a dict suitable
    for JSON storage; {"checked": False, "reason": ...} when not computable."""
    day = when_iso[:10]
    catalog = _nearest_catalog_date(day)
    if catalog is None:
        return {"checked": False,
                "reason": "no orbital data near this date yet"}
    from skyfield.api import load, wgs84
    ts = load.timescale()
    when = datetime.strptime(when_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    times = ts.from_datetimes([when + timedelta(minutes=m)
                               for m in range(-WINDOW_MIN, WINDOW_MIN + 1, 3)])
    observer = wgs84.latlon(lat, lon)
    eph = _ephemeris()
    sun_dark = None
    if eph is not None:
        from skyfield.api import N  # noqa: F401  (kept for clarity)
        astro = (eph["earth"] + observer).at(times).observe(eph["sun"]).apparent()
        sun_alt = astro.altaz()[0].degrees
        # visible-sky rule: any part of the window with sun below -6°
        sun_dark = sun_alt < -6

    bright = []
    starlink_batches: dict[str, list] = {}
    starlink_visible = 0
    for group, sat in _load_sats(catalog):
        try:
            topo = (sat - observer).at(times)
            alt, az, _ = topo.altaz()
        except Exception:
            continue
        alts = alt.degrees
        peak = int(alts.argmax())
        min_alt = STARLINK_MIN_ALT if group == "starlink" else BRIGHT_MIN_ALT
        if alts[peak] < min_alt:
            continue
        visible = True
        if eph is not None:
            sunlit = sat.at(times).is_sunlit(eph)
            visible = bool((sunlit & sun_dark & (alts > min_alt)).any())
        if not visible:
            continue
        entry = {
            "name": sat.name.strip(),
            "alt": round(float(alts[peak])),
            "az": _compass(float(az.degrees[peak])),
            "time": times[peak].utc_strftime("%H:%M"),
        }
        if group == "starlink":
            starlink_visible += 1
            starlink_batches.setdefault(sat.model.intldesg[:5], []).append(entry)
        else:
            bright.append(entry)
    bright.sort(key=lambda e: -e["alt"])
    trains = []
    for batch, members in starlink_batches.items():
        if len(members) >= TRAIN_MIN_SATS:
            azs = {m["az"] for m in members}
            trains.append({"batch": batch, "count": len(members),
                           "az": "/".join(sorted(azs)[:3]),
                           "time": min(m["time"] for m in members)})
    return {
        "checked": True,
        "catalog_date": catalog,
        "visibility_filtered": eph is not None,
        "bright": bright[:6],
        "starlink_visible": starlink_visible,
        "trains": sorted(trains, key=lambda t: -t["count"]),
    }
