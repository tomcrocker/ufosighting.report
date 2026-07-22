import hmac
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import auth, db, posting, r2, search
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
    pending = conn.execute(
        "SELECT COUNT(*) FROM sightings WHERE status='pending_review'"
    ).fetchone()[0]
    return templates.TemplateResponse(
        request, "admin.html",
        {"user": user, "hidden": hidden, "featured": featured, "pending": pending,
         "csrf_token": auth.csrf_for(user.id)},
    )


@router.post("/admin/sighting/{sighting_id}/delete")
async def admin_delete(
    request: Request, sighting_id: int,
    conn=Depends(db.get_db), user=Depends(require_admin),
):
    """Permanent deletion: DB row (media/comments/yt_jobs cascade), the R2
    objects, and the search doc. Unlike hide, there is no undo."""
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf_token", "")), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    row = conn.execute("SELECT id FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    media = conn.execute(
        "SELECT r2_key, thumb_key, display_key FROM media WHERE sighting_id=?",
        (sighting_id,)).fetchall()
    for m in media:
        for key in (m["r2_key"], m["thumb_key"], m["display_key"]):
            if key:
                try:
                    r2.delete_key(key)
                except Exception as exc:
                    print(f"admin delete: R2 key {key} failed: {exc}")
    conn.execute("DELETE FROM sightings WHERE id=?", (sighting_id,))
    conn.commit()
    search.delete_sightings([sighting_id])
    return RedirectResponse("/", status_code=303)


@router.get("/admin/status")
def system_status(request: Request, conn=Depends(db.get_db), user=Depends(require_admin)):
    """One-glance health: catches the silent failures (bot dev-list drops,
    dead xAI keys) that otherwise only surface as missing DMs days later."""
    from app import reddit, search
    from app.config import get_settings
    import httpx as _httpx
    s = get_settings()
    checks = []

    def check(name, fn, detail=""):
        try:
            ok, extra = fn()
            checks.append((name, ok, extra or detail))
        except Exception as exc:
            checks.append((name, False, str(exc)[:160]))

    check("Bot login (u/%s)" % s.script_username,
          lambda: (bool(reddit.script_token()), "posting + verify DMs OK"))
    if s.read_username:
        check("Read login (u/%s)" % s.read_username,
              lambda: (bool(reddit.read_token()), "ingest + sync OK"))
    def _meili():
        if not search.enabled():
            return False, "MEILI_URL not set"
        url, headers, _ = search._base()
        r = _httpx.get(f"{url}/health", headers=headers, timeout=5)
        return r.status_code == 200, "index reachable"
    check("Meilisearch", _meili)
    def _xai():
        from app import extract
        return bool(extract.extract_fields(
            "[TITLE]\nOrb over Phoenix at 9pm July 1 2025")), "extraction OK"
    check("xAI extraction", _xai)

    row = conn.execute("SELECT MAX(created_at) AS latest FROM sightings WHERE source='reddit'").fetchone()
    yt = dict(conn.execute("SELECT status, COUNT(*) FROM yt_jobs GROUP BY status"))
    pending = conn.execute("SELECT COUNT(*) FROM sightings WHERE status='pending_review'").fetchone()[0]
    facts = [
        ("Last ingested sighting", row["latest"] or "never"),
        ("Pending mod review", pending),
        ("YouTube queue", f"{yt.get('pending', 0)} pending / {yt.get('failed', 0)} failed"),
        ("Public sightings", conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE status IN "
            "('live','removed_on_reddit')").fetchone()[0]),
        ("Sky-checked sightings", conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE sky_events LIKE "
            "'%\"checked\": true%'").fetchone()[0]),
    ]
    return templates.TemplateResponse(
        request, "status.html",
        {"user": user, "checks": checks, "facts": facts,
         "csrf_token": auth.csrf_for(user.id)})


@router.get("/admin/analytics")
def analytics_page(request: Request, conn=Depends(db.get_db), user=Depends(require_admin)):
    from app import analytics
    return templates.TemplateResponse(
        request, "analytics.html", {"user": user, "stats": analytics.summary(conn)})


@router.get("/admin/review")
def review_queue(request: Request, conn=Depends(db.get_db), user=Depends(require_admin)):
    rows = conn.execute(
        "SELECT * FROM sightings WHERE status='pending_review' ORDER BY created_at"
    ).fetchall()
    # Flag submissions that share a submitter IP — a quick tell for one person
    # filing under several throwaway usernames (the queue only holds unverified
    # submissions, so this is where sockpuppets surface).
    ip_counts = Counter(r["submitter_ip"] for r in rows if r["submitter_ip"])
    media = {}
    for row in rows:
        media[row["id"]] = conn.execute(
            "SELECT r2_key, thumb_key, kind FROM media WHERE sighting_id=? ORDER BY sort_order",
            (row["id"],),
        ).fetchall()
    return templates.TemplateResponse(
        request, "review.html",
        {"user": user, "rows": rows, "media": media, "ip_counts": ip_counts,
         "csrf_token": auth.csrf_for(user.id)},
    )


@router.post("/admin/review/{sighting_id}/approve")
async def review_approve(request: Request, sighting_id: int,
                         conn=Depends(db.get_db), user=Depends(require_admin)):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf_token", "")), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    try:
        posting.post_sighting(conn, sighting_id, verified=False)
    except Exception as exc:
        print(f"approve post failed for {sighting_id}: {exc}")
    return RedirectResponse("/admin/review", status_code=303)


@router.post("/admin/review/{sighting_id}/reject")
async def review_reject(request: Request, sighting_id: int,
                        conn=Depends(db.get_db), user=Depends(require_admin)):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf_token", "")), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    conn.execute("UPDATE sightings SET status='rejected' WHERE id=?", (sighting_id,))
    conn.commit()
    search.delete_sightings([sighting_id])
    return RedirectResponse("/admin/review", status_code=303)


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
    if action == "hide":
        search.delete_sightings([sighting_id])
    else:
        search.index_sightings(conn, [sighting_id])
    return RedirectResponse(_safe_next(str(form.get("next", ""))), status_code=303)
