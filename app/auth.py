import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Session:
    id: str
    username: str
    access_token: str
    expires_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(
    conn: sqlite3.Connection, username: str, access_token: str, ttl_seconds: int
) -> str:
    sid = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(seconds=ttl_seconds)).strftime(ISO)
    conn.execute(
        "INSERT INTO sessions (id, username, access_token, expires_at) VALUES (?,?,?,?)",
        (sid, username, access_token, expires_at),
    )
    conn.commit()
    return sid


def get_session(conn: sqlite3.Connection, session_id: str) -> Session | None:
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now().strftime(ISO):
        delete_session(conn, session_id)
        return None
    return Session(row["id"], row["username"], row["access_token"], row["expires_at"])


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()


def csrf_for(session_id: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, b"csrf:" + session_id.encode(), hashlib.sha256).hexdigest()[:32]


def save_draft(conn: sqlite3.Connection, username: str, form: dict) -> None:
    conn.execute(
        """INSERT INTO drafts (username, form_json, updated_at)
           VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
           ON CONFLICT(username) DO UPDATE SET
             form_json=excluded.form_json, updated_at=excluded.updated_at""",
        (username, json.dumps(form)),
    )
    conn.commit()


def load_draft(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute("SELECT form_json FROM drafts WHERE username=?", (username,)).fetchone()
    return json.loads(row["form_json"]) if row else None


def delete_draft(conn: sqlite3.Connection, username: str) -> None:
    conn.execute("DELETE FROM drafts WHERE username=?", (username,))
    conn.commit()
