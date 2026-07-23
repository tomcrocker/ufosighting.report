import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates

from app import auth, db, helpers, mdrender, r2
from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["media_url"] = r2.public_url
templates.env.filters["duration_h"] = helpers.humanize_duration
templates.env.filters["post_date"] = helpers.post_date
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
templates.env.globals["media_base"] = get_settings().media_base_url
templates.env.globals["anonymous_enabled"] = get_settings().anonymous_enabled
templates.env.globals["ga_id"] = get_settings().ga_measurement_id


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
    # who counts as an admin for an existing session: anyone listed in
    # ADMIN_USERS (shared password) or ADMIN_CREDENTIALS (own password). The
    # basic-auth LOGIN in require_admin is what checks the per-user password.
    if not user:
        return False
    u = user.username.lower()
    s = get_settings()
    return u in s.admin_users or u in s.admin_credentials


def require_admin(request: Request, response: Response,
                  user: auth.Session | None = Depends(current_user),
                  conn=Depends(db.get_db)) -> auth.Session:
    """Admin gate: existing admin session, or HTTP Basic (ADMIN_PASSWORD env).
    A successful Basic login mints a normal session cookie so admin controls
    show up across the whole site, not just under /admin. With no
    ADMIN_PASSWORD configured the area stays hidden as a 404."""
    if is_admin(user):
        return user
    s = get_settings()
    if not s.admin_credentials:
        raise HTTPException(status_code=404)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        import base64
        import secrets as _secrets
        try:
            decoded = base64.b64decode(header[6:]).decode()
            username, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            username, password = "", ""
        stored = s.admin_credentials.get(username.lower())
        if stored is not None and _secrets.compare_digest(password, stored):
            sid = auth.create_session(conn, username.lower(), "basic", s.session_ttl_seconds)
            # routes return TemplateResponses, which discard cookies set on
            # the injected Response — the app middleware applies this instead
            request.state.set_sid = sid
            return auth.get_session(conn, sid)
    raise HTTPException(status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="moderators"'})
