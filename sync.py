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
import time

from app import comments, db, reddit, search, verify
from app.config import get_settings

HOT_WINDOW_HOURS = 72
FULL_WINDOW_HOURS = 30 * 24


def sync_once(conn, *, window_hours: int = HOT_WINDOW_HOURS,
              comment_sleep=time.sleep) -> dict:
    rows = conn.execute(
        """SELECT id, reddit_post_id, status, source FROM sightings
           WHERE reddit_post_id IS NOT NULL
             AND status IN ('live', 'removed_on_reddit', 'deleted_by_user', 'removed_by_mod')
             AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)""",
        (f"-{window_hours} hours",),
    ).fetchall()
    if not rows:
        return {"checked": 0, "updated": 0, "comments": 0}
    infos = reddit.fetch_posts_info([r["reddit_post_id"] for r in rows])
    updated = 0
    touched = []
    live_rows = []
    approve_token = None
    for r in rows:
        info = infos.get(r["reddit_post_id"])
        if info is None:
            continue
        # Self-rescue: the sitewide spam filter keeps removing the young bot
        # account's media posts. The bot (or the fallback mod account) can
        # approve our OWN posts (source='site'); mod removals are respected,
        # and ingested posts are never ours to approve.
        if r["source"] == "site":
            try:
                if approve_token is None:
                    # the personal mod account: stable login + full mod perms
                    # (the bot may lack the "posts" permission or app access)
                    approve_token = reddit.read_token()
                if info.removed_by_category == "reddit":
                    reddit.approve(approve_token, post_id=r["reddit_post_id"])
                    info = reddit.PostInfo(None, info.score, info.num_comments)
                    print(f"sync: self-approved spam-removed post {r['reddit_post_id']}")
                # the bot's pinned details comment gets spam-filtered too
                for cid in reddit.fetch_removed_bot_comments(
                        approve_token, r["reddit_post_id"],
                        get_settings().script_username):
                    reddit.approve(approve_token, comment_id=cid)
                    print(f"sync: self-approved removed bot comment {cid} "
                          f"on {r['reddit_post_id']}")
            except reddit.RedditError as exc:
                print(f"sync: self-approve of {r['reddit_post_id']} failed: {exc}")
        new_status = reddit.status_from_removed_by_category(info.removed_by_category)
        conn.execute(
            "UPDATE sightings SET reddit_score=?, reddit_num_comments=?, status=?, "
            "removed_by_category=? WHERE id=?",
            (info.score, info.num_comments, new_status, info.removed_by_category, r["id"]),
        )
        touched.append(r["id"])
        if new_status == "live":
            live_rows.append((r["id"], r["reddit_post_id"]))
        if new_status != r["status"]:
            updated += 1
    conn.commit()
    search.index_sightings(conn, touched)
    # Top-comments refresh rides along for still-live posts only — removed or
    # deleted posts keep their last-fetched comments as part of the archive.
    refreshed = 0
    try:
        if live_rows:
            token = reddit.read_token()
            for i, (sid, pid) in enumerate(live_rows):
                if comments.refresh_for_sighting(conn, token, sid, pid):
                    refreshed += 1
                if i < len(live_rows) - 1:
                    comment_sleep(1)
    except reddit.RedditError as exc:
        print(f"comment refresh skipped: {exc}")
    return {"checked": len(rows), "updated": updated, "comments": refreshed}


def main(full: bool = False) -> None:
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        window = FULL_WINDOW_HOURS if full else HOT_WINDOW_HOURS
        result = sync_once(conn, window_hours=window)
        swept = verify.sweep_pending_verify(conn, s.verify_window_hours)
        tier = "full" if full else "hot"
        print(f"sync[{tier}]: checked={result['checked']} "
              f"status_changes={result['updated']} "
              f"comments={result.get('comments', 0)} swept={swept}")
    finally:
        conn.close()


if __name__ == "__main__":
    main(full="--full" in sys.argv)
