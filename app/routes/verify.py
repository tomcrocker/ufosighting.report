from fastapi import APIRouter, Depends, Request

from app import appsettings, db, quality
from app.web import templates

router = APIRouter()


@router.get("/verify/{token}")
def verify_click(request: Request, token: str, conn=Depends(db.get_db)):
    """Confirm the reporter owns the account, then route the sighting.

    We used to post to Reddit inline here, which forced the post out before
    media processing finished — a video whose poster wasn't ready yet got
    dropped. Now the click queues it, and the worker posts once media is ready.

    Two gates decide whether it auto-queues or waits for a moderator: the global
    moderation hold, and a per-account CQS-proxy check (ban / new account / low
    karma). The bot is a trusted author, so without this a low-CQS or banned
    reporter would sail past the filter Reddit would normally apply to them.
    """
    row = conn.execute(
        "SELECT id, reddit_username FROM sightings "
        "WHERE verify_token=? AND status='pending_verify'",
        (token,),
    ).fetchone()
    if row is None:
        return templates.TemplateResponse(request, "verify_result.html", {"user": None, "ok": False})

    if appsettings.hold_posts(conn):
        to_review, reason = True, "moderation hold (all submissions held)"
    else:
        ok, reason = quality.gate(row["reddit_username"])
        to_review = not ok

    if to_review:
        conn.execute(
            """UPDATE sightings SET status='pending_review', username_verified=1,
                  verify_token=NULL, review_reason=? WHERE id=?""",
            (reason, row["id"]),
        )
        print(f"verify: sighting {row['id']} (u/{row['reddit_username']}) "
              f"sent to review — {reason}")
    else:
        conn.execute(
            """UPDATE sightings SET status='pending_post', username_verified=1,
                  verify_token=NULL,
                  pending_post_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?""",
            (row["id"],),
        )
    conn.commit()
    return templates.TemplateResponse(
        request, "verify_result.html",
        {"user": None, "ok": True, "queued": True, "held": to_review}
    )
