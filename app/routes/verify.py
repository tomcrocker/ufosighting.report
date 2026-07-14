from fastapi import APIRouter, Depends, Request

from app import db, helpers, indexnow, posting, reddit
from app.config import get_settings
from app.web import templates

router = APIRouter()


@router.get("/verify/{token}")
def verify_click(request: Request, token: str, conn=Depends(db.get_db)):
    row = conn.execute(
        "SELECT id, title FROM sightings WHERE verify_token=? AND status='pending_verify'",
        (token,),
    ).fetchone()
    if row is None:
        return templates.TemplateResponse(request, "verify_result.html", {"user": None, "ok": False})
    try:
        posting.post_sighting(conn, row["id"], verified=True)
    except reddit.RedditError:
        return templates.TemplateResponse(
            request, "verify_result.html", {"user": None, "ok": False, "retry": True}
        )
    url = f"/sighting/{row['id']}/{helpers.slugify(row['title'])}"
    # a freshly-verified sighting is a brand-new URL — ping IndexNow so Bing &
    # co. can index it immediately (best-effort, never blocks the response)
    try:
        indexnow.submit_url(f"{get_settings().base_url}{url}")
    except Exception:
        pass
    return templates.TemplateResponse(
        request, "verify_result.html", {"user": None, "ok": True, "url": url}
    )
