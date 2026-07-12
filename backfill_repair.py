"""Repair sightings ingested while xAI extraction was down (empty
location_text + post-time sighted_at fallback). Re-runs extraction + geocode
and updates the rows in place — media/comments/yt_jobs are untouched.

Text sources: the archive manifest (/tmp/sightings_export.jsonl) when the
post is in it (has op_comments), else the Reddit API via read_token.

    .venv/bin/python backfill_repair.py [--since 2026-07-11T23:20:00Z] [--dry]

Idempotent: only touches rows that still look unextracted, and only writes
fields the fresh extraction actually produced."""
import json
import os
import sys
import time
from datetime import datetime, timezone

import ingest
from app import db, extract, geocode, reddit, search
from app.config import get_settings

MANIFEST = "/tmp/sightings_export.jsonl"
ISO = "%Y-%m-%dT%H:%M:%SZ"


def load_manifest_texts() -> dict:
    out = {}
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            for line in f:
                r = json.loads(line)
                out[r["id"]] = r
    return out


def probe_xai() -> bool:
    got = extract.extract_fields("[TITLE]\nTest orb over Phoenix at 9pm July 1 2025")
    return bool(got)


def main(since: str, dry: bool) -> None:
    conn = db.connect(get_settings().db_path)
    manifest = load_manifest_texts()
    token = None
    rows = conn.execute(
        """SELECT id, reddit_post_id, title, description, created_at FROM sightings
           WHERE source='reddit' AND location_text = '' AND lat IS NULL
             AND created_at >= ? ORDER BY id""", (since,)).fetchall()
    print(f"repair: {len(rows)} candidate rows since {since}", flush=True)
    if not rows:
        return
    if not probe_xai():
        print("repair: xAI still failing — aborting, run again later")
        return
    fixed = skipped = 0
    touched = []
    for r in rows:
        pid = r["reddit_post_id"]
        m = manifest.get(pid)
        if m:
            text = extract.combine_post_text(
                {"title": m.get("title"), "selftext": m.get("selftext")},
                m.get("op_comments") or [])
            created = m.get("created_utc", 0)
        else:
            if token is None:
                token = reddit.read_token()
            post = reddit.fetch_post(token, pid)
            time.sleep(2)
            if not post:
                skipped += 1
                continue
            op = ingest.fetch_op_comments(token, post)
            text = extract.combine_post_text(post, op)
            created = post.get("created_utc", 0)
        post_created_iso = datetime.fromtimestamp(created, timezone.utc).strftime(ISO)
        clamped = extract.validate_and_clamp(extract.extract_fields(text),
                                             post_created_iso=post_created_iso)
        coords = None
        if clamped.get("location_text"):
            coords = geocode.forward(conn, clamped["location_text"])
        sighted_at, tz_name = ingest.build_sighted_at(clamped, post_created_iso)
        if not clamped.get("location_text") and not clamped.get("date"):
            skipped += 1  # genuinely nothing extractable
            continue
        if dry:
            print(f"  would fix {r['id']} ({pid}): loc={clamped.get('location_text')!r}")
            fixed += 1
            continue
        conn.execute(
            """UPDATE sightings SET location_text=?, city=?, country=?, lat=?, lon=?,
                 sighted_at=?, tz_name=? WHERE id=?""",
            (clamped.get("location_text") or "",
             (coords or {}).get("city") or clamped.get("city"),
             (coords or {}).get("country") or clamped.get("country"),
             (coords or {}).get("lat"), (coords or {}).get("lon"),
             sighted_at, tz_name, r["id"]))
        conn.commit()
        touched.append(r["id"])
        fixed += 1
        if fixed % 25 == 0:
            print(f"repair: {fixed} fixed so far", flush=True)
    for i in range(0, len(touched), 200):
        search.index_sightings(conn, touched[i:i + 200])
    print(f"repair done: fixed={fixed} skipped={skipped}", flush=True)
    conn.close()


if __name__ == "__main__":
    since = "2026-07-11T23:20:00Z"
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    main(since, dry="--dry" in sys.argv)
