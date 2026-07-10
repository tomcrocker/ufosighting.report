import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app import auth, db, reddit_oauth
from app.config import get_settings
from app.web import templates

router = APIRouter()


def _safe_next(next_url: str) -> str:
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@router.get("/auth/login")
def login(next: str = "/submit"):
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(reddit_oauth.login_url(state), status_code=302)
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax")
    resp.set_cookie("oauth_next", _safe_next(next), max_age=600, httponly=True, samesite="lax")
    return resp


@router.get("/auth/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    conn=Depends(db.get_db),
):
    def fail(message: str, status: int):
        return templates.TemplateResponse(
            request, "login.html",
            {"user": None, "error": message, "next_url": "/submit"},
            status_code=status,
        )

    if error or not code:
        return fail("Reddit login was cancelled or failed. You can try again.", 400)
    saved_state = request.cookies.get("oauth_state", "")
    next_url = request.cookies.get("oauth_next", "/")
    if not state or not saved_state or state != saved_state:
        return fail("Login session mismatch — please try again.", 400)
    try:
        token = reddit_oauth.exchange_code(code)
        username = reddit_oauth.fetch_username(token)
    except reddit_oauth.AuthError:
        return fail("Could not complete Reddit login — please try again.", 502)

    s = get_settings()
    sid = auth.create_session(conn, username, token, s.session_ttl_seconds)
    resp = RedirectResponse(_safe_next(next_url), status_code=303)
    resp.set_cookie(
        "sid", sid, max_age=s.session_ttl_seconds,
        httponly=True, samesite="lax", secure=s.base_url.startswith("https"),
    )
    resp.delete_cookie("oauth_state")
    resp.delete_cookie("oauth_next")
    return resp


@router.get("/auth/logout")
def logout(request: Request, conn=Depends(db.get_db)):
    sid = request.cookies.get("sid")
    if sid:
        auth.delete_session(conn, sid)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("sid")
    return resp
