"""Moderation sync — mirrors Reddit mod actions into the gallery.

Two tiers (both refresh reddit_score/num_comments, which ride along free in
the same /api/info response as removal status):
- HOT: every 15 min via ufosighting-sync.timer, posts < 72h old — live scores
  while voting is active, fast moderation mirroring.
- FULL: daily via ufosighting-sync-full.timer (`sync.py --full`), posts < 30
  days — keeps removal-mirroring alive after voting goes stale.

Reddit is the single source of moderation truth: removed posts hide their
gallery entries, approved posts bring them back. hidden_by_admin is site-side
state and is never auto-changed.
"""
import sys

from app import db, reddit, search, verify
from app.config import get_settings

HOT_WINDOW_HOURS = 72
FULL_WINDOW_HOURS = 30 * 24


def sync_once(conn, *, window_hours: int = HOT_WINDOW_HOURS) -> dict:
    rows = conn.execute(
        """SELECT id, reddit_post_id, status FROM sightings
           WHERE reddit_post_id IS NOT NULL
             AND status IN ('live', 'removed_on_reddit', 'deleted_by_user')
             AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)""",
        (f"-{window_hours} hours",),
    ).fetchall()
    if not rows:
        return {"checked": 0, "updated": 0}
    infos = reddit.fetch_posts_info([r["reddit_post_id"] for r in rows])
    updated = 0
    touched = []
    for r in rows:
        info = infos.get(r["reddit_post_id"])
        if info is None:
            continue
        new_status = reddit.status_from_removed_by_category(info.removed_by_category)
        conn.execute(
            "UPDATE sightings SET reddit_score=?, reddit_num_comments=?, status=? WHERE id=?",
            (info.score, info.num_comments, new_status, r["id"]),
        )
        touched.append(r["id"])
        if new_status != r["status"]:
            updated += 1
    conn.commit()
    search.index_sightings(conn, touched)
    return {"checked": len(rows), "updated": updated}


def main(full: bool = False) -> None:
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        window = FULL_WINDOW_HOURS if full else HOT_WINDOW_HOURS
        result = sync_once(conn, window_hours=window)
        swept = verify.sweep_pending_verify(conn, s.verify_window_hours)
        tier = "full" if full else "hot"
        print(f"sync[{tier}]: checked={result['checked']} "
              f"status_changes={result['updated']} swept={swept}")
    finally:
        conn.close()


if __name__ == "__main__":
    main(full="--full" in sys.argv)
