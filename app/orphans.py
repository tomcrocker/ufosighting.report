"""Orphaned uploads: files a reporter successfully uploaded that never made it
onto a sighting.

The upload is a browser-direct PUT to R2, so it never touches our server. If
the browser then drops the file before submit (a stray "remove" click, a UI
hiccup), the bytes sit in R2 forever with nothing pointing at them and no error
anywhere. That's how sighting 12618 lost a 127MB video.

`upload_keys` records every presigned key we issue, which is what makes the
gap visible: a key with no matching media row is an orphan.
"""

# Long enough that a normal session (upload, finish the wizard, submit) is
# never mistaken for an abandoned file.
DEFAULT_MIN_AGE_MINUTES = 60


def record_key(conn, *, key: str, ip: str, kind: str) -> None:
    """Note that we handed out an upload URL for this key."""
    conn.execute(
        "INSERT OR IGNORE INTO upload_keys (key, ip, kind) VALUES (?,?,?)",
        (key, ip, kind))
    conn.commit()


def find(conn, *, min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES, limit: int = 200):
    """Presigned keys with no media row, older than the grace period.

    Cheap: a single left join, no R2 listing. A key may legitimately be
    missing from R2 (the PUT itself failed), so callers that act on these
    should confirm the object exists before drawing conclusions.
    """
    return conn.execute(
        """SELECT u.key, u.kind, u.ip, u.created_at
             FROM upload_keys u
             LEFT JOIN media m ON m.r2_key = u.key
            WHERE m.id IS NULL
              AND u.created_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
            ORDER BY u.created_at DESC
            LIMIT ?""",
        (f"-{min_age_minutes} minutes", limit),
    ).fetchall()


def count(conn, *, min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM upload_keys u
             LEFT JOIN media m ON m.r2_key = u.key
            WHERE m.id IS NULL
              AND u.created_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (f"-{min_age_minutes} minutes",),
    ).fetchone()[0]


def warn_for_submission(conn, *, ip: str, attached: list[str], sighting_id: int) -> list[str]:
    """Log keys this IP uploaded in the last hour but didn't attach.

    Runs at submit time, when we still know who uploaded what — so a dropped
    file is caught immediately and tied to a specific sighting, instead of
    surfacing days later as an anonymous blob in R2.
    """
    recent = conn.execute(
        """SELECT key, kind FROM upload_keys
            WHERE ip = ?
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hours')""",
        (ip,),
    ).fetchall()
    missing = [r["key"] for r in recent if r["key"] not in set(attached)]
    if missing:
        print(f"orphan-upload: sighting {sighting_id} left {len(missing)} uploaded "
              f"file(s) unattached: {', '.join(missing)}")
    return missing
