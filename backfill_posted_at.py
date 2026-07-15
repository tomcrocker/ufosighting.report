"""One-shot: populate reddit_posted_at (ISO-UTC of the Reddit submission time)
for every sighting that has a Reddit post but no stored post date. Safe to
re-run — only touches rows where reddit_posted_at IS NULL. created_utc rides
along free in the same /api/info response sync.py already uses; 1s throttle
between batches (the script app is shared with ufosarchive)."""
import time

from app import db, helpers, reddit
from app.config import get_settings

BATCH = 100
MAX_RETRIES = 8
BATCH_PAUSE = 4  # gentle base pace — the Reddit app is shared with ufosarchive


def _fetch_with_retry(ids, *, sleep):
    """The Reddit client id is shared with ufosarchive's always-on collector,
    so /api/info 429s intermittently. Back off and retry the batch rather than
    aborting the whole run; re-raise anything that isn't a rate limit."""
    delay = 30
    for attempt in range(MAX_RETRIES):
        try:
            return reddit.fetch_posts_info(ids)
        except reddit.RedditError as exc:
            if "429" not in str(exc) or attempt == MAX_RETRIES - 1:
                raise
            sleep(delay)
            delay = min(delay * 2, 120)
    return {}


def backfill(conn, *, sleep=time.sleep) -> dict:
    rows = conn.execute(
        """SELECT id, reddit_post_id FROM sightings
           WHERE reddit_post_id IS NOT NULL AND reddit_posted_at IS NULL
           ORDER BY id""").fetchall()
    updated = 0
    batches = range(0, len(rows), BATCH)
    for n, i in enumerate(batches, 1):
        chunk = rows[i:i + BATCH]
        infos = _fetch_with_retry([r["reddit_post_id"] for r in chunk], sleep=sleep)
        for r in chunk:
            info = infos.get(r["reddit_post_id"])
            iso = helpers.iso_from_epoch(info.created_utc) if info else None
            if iso is None:
                continue
            conn.execute("UPDATE sightings SET reddit_posted_at=? WHERE id=?",
                         (iso, r["id"]))
            updated += 1
        conn.commit()
        print(f"backfill_posted_at: batch {n}/{len(batches)} "
              f"updated={updated}/{len(rows)}", flush=True)
        if i + BATCH < len(rows):
            sleep(BATCH_PAUSE)
    return {"candidates": len(rows), "updated": updated}


if __name__ == "__main__":
    conn = db.connect(get_settings().db_path)
    try:
        result = backfill(conn)
        print(f"backfill_posted_at: candidates={result['candidates']} "
              f"updated={result['updated']}")
    finally:
        conn.close()
