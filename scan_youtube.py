"""One-shot retroactive repair: enqueue YouTube downloads for already-ingested
reddit sightings that have no media.

Body links are found in the stored description; link-post URLs were never
persisted, so those posts are re-fetched from the Reddit API (2s throttle —
the script app is shared with ufosarchive). Idempotent: yt_jobs.sighting_id
is UNIQUE and rows with media or an existing job are skipped."""
import time

from app import db, reddit, ytdetect
from app.config import get_settings

API_SLEEP_SECONDS = 2


def scan(conn, *, fetch=None, sleep=time.sleep) -> dict:
    rows = conn.execute(
        """SELECT s.id, s.reddit_post_id, s.description FROM sightings s
           WHERE s.source='reddit'
             AND NOT EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id)
             AND NOT EXISTS (SELECT 1 FROM yt_jobs j WHERE j.sighting_id = s.id)
           ORDER BY s.id""").fetchall()
    stats = {"scanned": len(rows), "body_hits": 0, "api_hits": 0, "enqueued": 0}
    token = None
    for r in rows:
        url = ytdetect.find_in_text(r["description"])
        if url:
            stats["body_hits"] += 1
        elif r["reddit_post_id"]:
            if fetch is not None:
                post = fetch(r["reddit_post_id"])
            else:
                if token is None:
                    token = reddit.read_token()
                post = reddit.fetch_post(token, r["reddit_post_id"])
            sleep(API_SLEEP_SECONDS)
            url = ytdetect.find_youtube_url(post) if post else None
            if url:
                stats["api_hits"] += 1
        if url:
            conn.execute("INSERT OR IGNORE INTO yt_jobs (sighting_id, url) VALUES (?,?)",
                         (r["id"], url))
            stats["enqueued"] += 1
    conn.commit()
    return stats


if __name__ == "__main__":
    c = db.connect(get_settings().db_path)
    try:
        print("scan_youtube:", scan(c))
    finally:
        c.close()
