"""One-shot: reclassify existing 'removed_on_reddit' posts by their REAL Reddit
removal reason. Genuine mod removals (removed_by_category = moderator, plus
Reddit's T&S / legal takedowns) become 'removed_by_mod' (hidden); spam-filter
and modqueue-pending ones stay 'removed_on_reddit' (still visible); anything a
mod has since approved flips back to 'live'. Stores the raw reason either way.

Safe to re-run — only reads rows currently 'removed_on_reddit'. The Reddit app
is shared with ufosarchive, so /api/info 429s intermittently: back off + retry.
Run `reindex.py --wipe` afterwards so Meilisearch drops the now-hidden posts.
"""
import time
from collections import Counter

from app import db, reddit
from app.config import get_settings

BATCH = 100
MAX_RETRIES = 8


def _fetch_with_retry(ids, *, sleep):
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


def reclassify(conn, *, sleep=time.sleep) -> dict:
    rows = conn.execute(
        "SELECT id, reddit_post_id FROM sightings "
        "WHERE status='removed_on_reddit' AND reddit_post_id IS NOT NULL "
        "ORDER BY id").fetchall()
    outcomes = Counter()
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        infos = _fetch_with_retry([r["reddit_post_id"] for r in chunk], sleep=sleep)
        for r in chunk:
            info = infos.get(r["reddit_post_id"])
            if info is None:
                outcomes["not_returned"] += 1
                continue
            rbc = info.removed_by_category
            new_status = reddit.status_from_removed_by_category(rbc)
            conn.execute(
                "UPDATE sightings SET status=?, removed_by_category=? WHERE id=?",
                (new_status, rbc, r["id"]))
            outcomes[new_status] += 1
        conn.commit()
        n = i // BATCH + 1
        print(f"backfill_removed_reasons: batch {n}/{(len(rows) + BATCH - 1) // BATCH} "
              f"{dict(outcomes)}", flush=True)
        if i + BATCH < len(rows):
            sleep(3)
    return {"total": len(rows), "outcomes": dict(outcomes)}


if __name__ == "__main__":
    conn = db.connect(get_settings().db_path)
    try:
        result = reclassify(conn)
        print(f"\nbackfill_removed_reasons DONE: {result}")
        print("now run: reindex.py --wipe")
    finally:
        conn.close()
