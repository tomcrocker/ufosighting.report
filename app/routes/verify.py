from fastapi import APIRouter, Depends, Request

from app import appsettings, db
from app.web import templates

router = APIRouter()


@router.get("/verify/{token}")
def verify_click(request: Request, token: str, conn=Depends(db.get_db)):
    """Confirm the reporter owns the account and queue the sighting.

    We used to post to Reddit inline here, which forced the post to go out
    before media processing had finished — any video whose poster frame wasn't
    ready yet was silently dropped from the post. Now the click just queues it
    and the background worker posts once the media is actually ready.
    """
    row = conn.execute(
        "SELECT id FROM sightings WHERE verify_token=? AND status='pending_verify'",
        (token,),
    ).fetchone()
    if row is None:
        return templates.TemplateResponse(request, "verify_result.html", {"user": None, "ok": False})
    conn.execute(
        """UPDATE sightings
              SET status='pending_post', username_verified=1, verify_token=NULL,
                  pending_post_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
            WHERE id=?""",
        (row["id"],),
    )
    conn.commit()
    return templates.TemplateResponse(
        request, "verify_result.html",
        {"user": None, "ok": True, "queued": True,
         "held": appsettings.hold_posts(conn)}
    )
