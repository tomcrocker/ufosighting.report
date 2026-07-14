"""Daily cleanup — orphaned R2 uploads, expired sessions, stale drafts,
abandoned pending_post rows. Run by ufosighting-cleanup.timer."""
from datetime import datetime, timedelta, timezone

from app import db, r2
from app.config import get_settings


def cleanup_uploads(conn, older_than_hours: int = 48) -> int:
    referenced: set[str] = set()
    for row in conn.execute("SELECT r2_key, thumb_key FROM media"):
        referenced.add(row["r2_key"])
        if row["thumb_key"]:
            referenced.add(row["thumb_key"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    deleted = 0
    for key, last_modified in r2.list_keys("uploads/"):
        if key not in referenced and last_modified < cutoff:
            r2.delete_key(key)
            deleted += 1
    return deleted


def cleanup_sessions(conn) -> int:
    cur = conn.execute(
        "DELETE FROM sessions WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    )
    conn.commit()
    return cur.rowcount


def cleanup_drafts(conn, older_than_days: int = 7) -> int:
    cur = conn.execute(
        "DELETE FROM drafts WHERE updated_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)",
        (f"-{older_than_days} days",),
    )
    conn.commit()
    return cur.rowcount


def cleanup_pending(conn, older_than_hours: int = 1) -> int:
    cur = conn.execute(
        """DELETE FROM sightings WHERE status = 'pending_post'
           AND created_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (f"-{older_than_hours} hours",),
    )
    conn.commit()
    return cur.rowcount


def cleanup_rate_events(conn, older_than_hours: int = 24) -> int:
    cur = conn.execute(
        "DELETE FROM rate_events WHERE created_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)",
        (f"-{older_than_hours} hours",),
    )
    conn.commit()
    return cur.rowcount


def fetch_tles() -> int:
    """Daily orbital-catalog snapshot — keeps our TLE archive gap-free even
    on days with no new geocoded sightings."""
    from app import satellites
    try:
        return len(satellites.fetch_today())
    except Exception as exc:
        print(f"cleanup: TLE fetch failed: {exc}")
        return 0


def fetch_launches() -> int:
    """Daily launch-cache top-up: last week (late NET slips) through next
    two days, merged into data/launches.json."""
    from datetime import datetime, timedelta, timezone

    from app import launches
    now = datetime.now(timezone.utc)
    try:
        return launches.fetch_range((now - timedelta(days=7)).isoformat(),
                                    (now + timedelta(days=2)).isoformat())
    except Exception as exc:
        print(f"cleanup: launch fetch failed: {exc}")
        return 0


def main() -> None:
    conn = db.connect(get_settings().db_path)
    try:
        print(
            "cleanup: "
            f"uploads={cleanup_uploads(conn)} "
            f"sessions={cleanup_sessions(conn)} "
            f"drafts={cleanup_drafts(conn)} "
            f"pending={cleanup_pending(conn)} "
            f"rate_events={cleanup_rate_events(conn)} "
            f"tles={fetch_tles()} "
            f"launches={fetch_launches()}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
