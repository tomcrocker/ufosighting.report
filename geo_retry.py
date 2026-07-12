"""Retry geocoding for public sightings that have a location_text (or an
extracted city) but no pin — using geocode.candidates() to simplify the
verbose LLM strings Nominatim rejects. Bypasses the negative geocode cache
(that's exactly what we're retrying) via search() directly, and reindexes
touched rows.

    nohup .venv/bin/python geo_retry.py > /tmp/geo_retry.log 2>&1 &
"""
import httpx

from app import db, geocode, search
from app.config import get_settings

if __name__ == "__main__":
    conn = db.connect(get_settings().db_path)
    rows = conn.execute(
        """SELECT id, location_text, city, country FROM sightings
           WHERE status IN ('live','deleted_by_user','removed_on_reddit')
             AND lat IS NULL
             AND (location_text != '' OR (city IS NOT NULL AND city != ''))
           ORDER BY id""").fetchall()
    print(f"geo_retry: {len(rows)} rows to try", flush=True)
    fixed = 0
    touched = []
    for n, r in enumerate(rows, 1):
        best = None
        for q in geocode.candidates(r["location_text"], r["city"], r["country"]):
            try:
                hits = geocode.search(q, limit=1)
            except httpx.HTTPError:
                continue
            if hits:
                best = hits[0]
                break
        if best:
            conn.execute(
                "UPDATE sightings SET lat=?, lon=?, city=COALESCE(NULLIF(?,''), city), "
                "country=COALESCE(NULLIF(?,''), country) WHERE id=?",
                (best["lat"], best["lon"], best["city"], best["country"], r["id"]))
            conn.commit()
            touched.append(r["id"])
            fixed += 1
        if n % 50 == 0:
            print(f"geo_retry: {n}/{len(rows)} tried, fixed={fixed}", flush=True)
    for i in range(0, len(touched), 200):
        search.index_sightings(conn, touched[i:i + 200])
    print(f"geo_retry done: fixed={fixed} of {len(rows)}", flush=True)
    conn.close()
