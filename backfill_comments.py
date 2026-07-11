"""One-shot: seed top comments for every public sighting with a reddit post.
Safe to re-run. 2s throttle — the script app is shared with ufosarchive."""
import time

from app import comments, db, reddit
from app.config import get_settings

if __name__ == "__main__":
    conn = db.connect(get_settings().db_path)
    try:
        token = reddit.script_token()
        rows = conn.execute(
            """SELECT id, reddit_post_id FROM sightings
               WHERE reddit_post_id IS NOT NULL
                 AND status IN ('live','deleted_by_user','removed_on_reddit')
               ORDER BY id""").fetchall()
        total = 0
        for r in rows:
            n = comments.refresh_for_sighting(conn, token, r["id"], r["reddit_post_id"])
            total += 1 if n else 0
            time.sleep(2)
        print(f"backfill_comments: posts={len(rows)} with_comments={total}")
    finally:
        conn.close()
