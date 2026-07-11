"""Moderation sync — mirrors Reddit mod actions into the gallery.

Run every 15 minutes by ufosighting-sync.timer. Reddit is the single source
of moderation truth: removed posts hide their gallery entries, approved posts
bring them back. hidden_by_admin is site-side state and is never auto-changed.
"""
from app import db, reddit, verify
from app.config import get_settings


def sync_once(conn) -> dict:
    rows = conn.execute(
        """SELECT id, reddit_post_id, status FROM sightings
           WHERE reddit_post_id IS NOT NULL
             AND status IN ('live', 'removed_on_reddit', 'deleted_by_user')
             AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')"""
    ).fetchall()
    if not rows:
        return {"checked": 0, "updated": 0}
    infos = reddit.fetch_posts_info([r["reddit_post_id"] for r in rows])
    updated = 0
    for r in rows:
        info = infos.get(r["reddit_post_id"])
        if info is None:
            continue
        new_status = reddit.status_from_removed_by_category(info.removed_by_category)
        conn.execute(
            "UPDATE sightings SET reddit_score=?, reddit_num_comments=?, status=? WHERE id=?",
            (info.score, info.num_comments, new_status, r["id"]),
        )
        if new_status != r["status"]:
            updated += 1
    conn.commit()
    return {"checked": len(rows), "updated": updated}


def main() -> None:
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        result = sync_once(conn)
        swept = verify.sweep_pending_verify(conn, s.verify_window_hours)
        print(f"sync: checked={result['checked']} status_changes={result['updated']} swept={swept}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
