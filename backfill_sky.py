"""Phase 2 sky backfill: historical satellite passes for the whole archive.

Space-Track's gp_history is the only source of historical orbital elements
(no API keys — account email/password login sets a session cookie). Per
sighting date we fetch two catalogs (all Starlink + the curated bright list),
cache them into data/tle/ in the same format Phase 1 uses, then compute
sky_events for that day's sightings with the existing satellites.passes_for.

Rate limits are 30/min & 300/hr — we send ~4/min. Roughly 2 queries/day x
~365 days ≈ 3h of fetching, resume-safe (cached catalogs are never refetched).

    nohup .venv/bin/python backfill_sky.py > /tmp/sky_backfill.log 2>&1 &
"""
import json
import os
import re
import time

import httpx

from app import db, satellites
from app.config import get_settings

BASE = "https://www.space-track.org"
QUERY_SLEEP = 16  # seconds between queries — ~4/min, far under their limits
PUBLIC = "('live','deleted_by_user','removed_on_reddit')"


def login(client: httpx.Client) -> None:
    user = os.environ["SPACETRACK_USER"]
    resp = client.post(f"{BASE}/ajaxauth/login",
                       data={"identity": user, "password": os.environ["SPACETRACK_PASS"]},
                       timeout=30)
    if resp.status_code != 200 or '"Failed"' in resp.text:
        raise SystemExit(f"space-track login failed: HTTP {resp.status_code} {resp.text[:200]}")
    print(f"logged in as {user}", flush=True)


def _dedupe_3le(text: str) -> str:
    """Keep one (latest-epoch) element set per satellite."""
    best: dict[str, tuple[str, str, str]] = {}
    lines = text.splitlines()
    i = 0
    while i + 2 < len(lines) + 1:
        if i + 2 >= len(lines) + 1:
            break
        name = lines[i] if lines[i].startswith("0 ") else None
        if name is None:
            i += 1
            continue
        l1, l2 = lines[i + 1], lines[i + 2]
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            i += 1
            continue
        satnum = l1[2:7]
        epoch = l1[18:32]
        if satnum not in best or epoch > best[satnum][0]:
            best[satnum] = (epoch, name, l1 + "\n" + l2)
        i += 3
    return "\n".join(f"{name}\n{tle}" for _, name, tle in best.values()) + "\n"


def bright_norad_ids() -> str:
    """NORAD ids from the newest cached visual+stations catalogs (today's
    bright list applied historically — close enough for a year)."""
    ids = set()
    for f in sorted(os.listdir(satellites.TLE_DIR)):
        if f.startswith(("visual-", "stations-")):
            for line in open(os.path.join(satellites.TLE_DIR, f)):
                if line.startswith("1 "):
                    ids.add(line[2:7].strip())
    return ",".join(sorted(ids))


def fetch_day(client: httpx.Client, day: str, ids: str) -> bool:
    next_day = time.strftime("%Y-%m-%d", time.gmtime(
        time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400))
    jobs = [
        (f"starlink-{day}.tle",
         f"{BASE}/basicspacedata/query/class/gp_history/OBJECT_NAME/~~STARLINK/"
         f"EPOCH/{day}--{next_day}/format/3le"),
        (f"visual-{day}.tle",
         f"{BASE}/basicspacedata/query/class/gp_history/NORAD_CAT_ID/{ids}/"
         f"EPOCH/{day}--{next_day}/format/3le"),
    ]
    for fname, url in jobs:
        path = os.path.join(satellites.TLE_DIR, fname)
        if os.path.exists(path):
            continue
        resp = client.get(url, timeout=180)
        if resp.status_code != 200 or resp.text.lstrip().startswith("<"):
            print(f"  fetch failed for {fname}: HTTP {resp.status_code}", flush=True)
            return False
        if "1 " not in resp.text[:400]:
            print(f"  no elements for {fname} (empty day?)", flush=True)
        with open(path, "w") as f:
            f.write(_dedupe_3le(resp.text))
        time.sleep(QUERY_SLEEP)
    return True


def main() -> None:
    conn = db.connect(get_settings().db_path)
    rows = conn.execute(
        f"""SELECT id, lat, lon, sighted_at FROM sightings
            WHERE lat IS NOT NULL AND status IN {PUBLIC}
              AND (sky_events IS NULL OR sky_events LIKE '%no orbital data%')
            ORDER BY sighted_at DESC"""
    ).fetchall()
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["sighted_at"][:10], []).append(r)
    print(f"{len(rows)} sightings across {len(by_day)} days", flush=True)

    ids = bright_norad_ids()
    client = httpx.Client(follow_redirects=True)
    login(client)
    done_s = done_d = 0
    for day, day_rows in sorted(by_day.items(), reverse=True):
        if not fetch_day(client, day, ids):
            print(f"skipping {day} (fetch failure)", flush=True)
            continue
        for r in day_rows:
            try:
                out = satellites.passes_for(r["lat"], r["lon"], r["sighted_at"])
            except Exception as exc:
                out = {"checked": False, "reason": f"computation failed: {exc}"[:160]}
            conn.execute("UPDATE sightings SET sky_events=? WHERE id=?",
                         (json.dumps(out), r["id"]))
            conn.commit()
            done_s += 1
        done_d += 1
        if done_d % 10 == 0:
            print(f"progress: {done_d}/{len(by_day)} days, {done_s} sightings", flush=True)
    print(f"sky backfill done: {done_s} sightings across {done_d} days", flush=True)


if __name__ == "__main__":
    main()
