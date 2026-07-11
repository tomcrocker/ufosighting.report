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
