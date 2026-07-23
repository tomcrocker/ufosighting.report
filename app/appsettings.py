"""Runtime settings a moderator can flip from /admin without a redeploy.

Env-based config (app.config) is for deploy-time facts; this is for switches
that need to change live, like the moderation hold that routes submissions into
the review queue during a troll wave.
"""

HOLD_POSTS = "hold_posts"  # "1" => verified submissions wait in the review queue


def get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set(conn, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET
               value=excluded.value,
               updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (key, value))
    conn.commit()


def hold_posts(conn) -> bool:
    """Are new submissions being held for manual approval instead of auto-posting?"""
    return get(conn, HOLD_POSTS) == "1"
