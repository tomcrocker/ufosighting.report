"""Archive-fed backfill: ingest sightings from a JSONL manifest exported by
deploy/local-vm/export_sightings.py (ufosarchive DB + media pre-uploaded to
R2 by the dev VM). Zero Reddit API calls — extraction uses archived OP
comments, media rows reference pre-uploaded keys, top comments come along.

    nohup .venv/bin/python backfill_archive.py /tmp/sightings_export.jsonl \
        > /tmp/backfill12.log 2>&1 &

Idempotent: rows whose reddit_post_id already exists are skipped."""
import json
import sys
from datetime import datetime, timezone

import ingest
from app import db, extract, geocode, search
from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
PROGRESS_EVERY = 25


def load_manifest(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _ingest_row(conn, row: dict) -> None:
    post_created_iso = datetime.fromtimestamp(
        row.get("created_utc", 0), timezone.utc).strftime(ISO)
    text = extract.combine_post_text(
        {"title": row.get("title"), "selftext": row.get("selftext")},
        row.get("op_comments") or [])
    clamped = extract.validate_and_clamp(extract.extract_fields(text),
                                         post_created_iso=post_created_iso)
    coords = None
    if clamped.get("location_text"):
        coords = geocode.forward(conn, clamped["location_text"])
    sighted_at, tz_name = ingest.build_sighted_at(clamped, post_created_iso)
    cur = conn.execute(
        """INSERT INTO sightings
             (source, reddit_username, title, description, sighted_at, tz_name,
              location_text, city, country,
              lat, lon, reddit_post_id, reddit_score, reddit_num_comments, status)
           VALUES ('reddit',?,?,?,?,?,?,?,?,?,?,?,?,?, 'live')""",
        (row.get("author") or "unknown",
         (row.get("title") or "Untitled sighting")[:300],
         (row.get("selftext") or "").strip() or (clamped.get("summary") or ""),
         sighted_at, tz_name,
         clamped.get("location_text") or "",
         (coords or {}).get("city") or clamped.get("city"),
         (coords or {}).get("country") or clamped.get("country"),
         (coords or {}).get("lat"), (coords or {}).get("lon"),
         row["id"], int(row.get("score") or 0), int(row.get("num_comments") or 0)),
    )
    sid = cur.lastrowid
    for i, m in enumerate(row.get("media") or []):
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) "
                     "VALUES (?,?,?,?)", (sid, m["key"], m["kind"], i))
    if row.get("yt_url") and not row.get("media"):
        conn.execute("INSERT OR IGNORE INTO yt_jobs (sighting_id, url) VALUES (?,?)",
                     (sid, row["yt_url"]))
    for c in (row.get("top_comments") or [])[:10]:
        conn.execute(
            "INSERT OR REPLACE INTO comments (reddit_comment_id, sighting_id, author,"
            " body, score, created_utc, permalink) VALUES (?,?,?,?,?,?,?)",
            (c["id"], sid, c.get("author") or "unknown", c.get("body") or "",
             int(c.get("score") or 0), int(c.get("created_utc") or 0),
             c.get("permalink") or ""))
    conn.commit()
    search.index_sightings(conn, [sid])


def run(conn, rows: list[dict]) -> dict:
    existing = {r["reddit_post_id"] for r in conn.execute(
        "SELECT reddit_post_id FROM sightings WHERE reddit_post_id IS NOT NULL")}
    stats = {"rows": len(rows), "added": 0, "skipped_existing": 0}
    for n, row in enumerate(rows, 1):
        if row["id"] in existing:
            stats["skipped_existing"] += 1
        else:
            try:
                _ingest_row(conn, row)
                stats["added"] += 1
            except Exception as exc:
                conn.rollback()
                print(f"backfill_archive: row {row['id']} failed: {exc}", flush=True)
        if n % PROGRESS_EVERY == 0:
            print(f"backfill_archive: {n}/{len(rows)} processed, "
                  f"added={stats['added']}", flush=True)
    return stats


if __name__ == "__main__":
    conn = db.connect(get_settings().db_path)
    try:
        print("backfill_archive done:", run(conn, load_manifest(sys.argv[1])))
    finally:
        conn.close()
