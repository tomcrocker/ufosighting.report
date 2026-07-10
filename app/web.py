from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import auth, db, r2
from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["media_url"] = r2.public_url


def current_user(request: Request, conn=Depends(db.get_db)) -> auth.Session | None:
    sid = request.cookies.get("sid")
    return auth.get_session(conn, sid) if sid else None


def is_admin(user: auth.Session | None) -> bool:
    return bool(user) and user.username.lower() in get_settings().admin_users


def require_admin(user: auth.Session | None = Depends(current_user)) -> auth.Session:
    if not is_admin(user):
        raise HTTPException(status_code=404)
    return user
