import sqlite3


def record(conn: sqlite3.Connection, ip: str, action: str) -> None:
    conn.execute("INSERT INTO rate_events (ip, action) VALUES (?,?)", (ip, action))
    conn.commit()


def count_recent(conn: sqlite3.Connection, ip: str, action: str, window_hours: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM rate_events
           WHERE ip=? AND action=?
             AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (ip, action, f"-{window_hours} hours"),
    ).fetchone()[0]


def allowed(conn, ip: str, action: str, limit: int, window_hours: int = 1) -> bool:
    return count_recent(conn, ip, action, window_hours) < limit


def retry_after_minutes(conn, ip: str, action: str, window_hours: int = 1) -> int:
    """Minutes until the most recent event ages out of the window — i.e. how long
    until a limit-1 gate opens again. 0 if there is no recent event."""
    row = conn.execute(
        """SELECT CAST((julianday(created_at, ?) - julianday('now')) * 1440 AS INTEGER)
           FROM rate_events WHERE ip=? AND action=? ORDER BY id DESC LIMIT 1""",
        (f"+{window_hours} hours", ip, action)).fetchone()
    return max(0, row[0]) if row and row[0] is not None else 0
