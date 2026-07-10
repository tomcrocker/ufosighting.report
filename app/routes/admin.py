import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import auth, db
from app.web import require_admin, templates

router = APIRouter()

ACTIONS = {
    "hide": ("status", "hidden_by_admin"),
    "unhide": ("status", "live"),
    "feature": ("featured", 1),
    "unfeature": ("featured", 0),
}


def _safe_next(next_url: str) -> str:
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/admin"


@router.get("/admin")
def admin_home(request: Request, conn=Depends(db.get_db), user=Depends(require_admin)):
    hidden = conn.execute(
        """SELECT * FROM sightings WHERE status != 'live'
           ORDER BY created_at DESC LIMIT 100"""
    ).fetchall()
    featured = conn.execute(
        """SELECT * FROM sightings WHERE featured = 1 AND status = 'live'
           ORDER BY created_at DESC"""
    ).fetchall()
    return templates.TemplateResponse(
        request, "admin.html",
        {"user": user, "hidden": hidden, "featured": featured,
         "csrf_token": auth.csrf_for(user.id)},
    )


@router.post("/admin/sighting/{sighting_id}/action")
async def admin_action(
    request: Request, sighting_id: int,
    conn=Depends(db.get_db), user=Depends(require_admin),
):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf_token", "")), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    action = str(form.get("action", ""))
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    column, value = ACTIONS[action]
    conn.execute(f"UPDATE sightings SET {column} = ? WHERE id = ?", (value, sighting_id))
    conn.commit()
    return RedirectResponse(_safe_next(str(form.get("next", ""))), status_code=303)
