import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import auth, db, helpers, mdrender, r2
from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["media_url"] = r2.public_url
templates.env.filters["duration_h"] = helpers.humanize_duration
templates.env.filters["reddit_md"] = mdrender.reddit_md
templates.env.globals["slugify"] = helpers.slugify


def _static_version() -> str:
    """Cache-buster derived from static file mtimes: changes on every deploy,
    so Cloudflare/browsers fetch fresh CSS/JS (nginx serves /static with a 7d
    cache — the ufosarchive stale-CSS lesson)."""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    latest = 0
    for f in static_dir.rglob("*"):
        if f.is_file():
            latest = max(latest, int(f.stat().st_mtime))
    return str(latest)


templates.env.globals["static_v"] = _static_version()
templates.env.globals["base_url"] = get_settings().base_url


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def current_user(request: Request, conn=Depends(db.get_db)) -> auth.Session | None:
    sid = request.cookies.get("sid")
    return auth.get_session(conn, sid) if sid else None


def is_admin(user: auth.Session | None) -> bool:
    return bool(user) and user.username.lower() in get_settings().admin_users


def require_admin(user: auth.Session | None = Depends(current_user)) -> auth.Session:
    if not is_admin(user):
        raise HTTPException(status_code=404)
    return user
