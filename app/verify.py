import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def verify_message(username: str, verify_url: str) -> tuple[str, str]:
    subject = "Confirm your UFO sighting submission"
    text = (
        f"Hi u/{username},\n\n"
        f"Did you just submit a UFO sighting on ufosighting.report? "
        f"Confirm it was you and it will be posted right away:\n\n{verify_url}\n\n"
        f"If this wasn't you, you can safely ignore this message — nothing will be posted."
    )
    return subject, text


def sweep_pending_verify(conn, window_hours: int) -> int:
    cur = conn.execute(
        """UPDATE sightings SET status='pending_review'
           WHERE status='pending_verify'
             AND verify_sent_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (f"-{window_hours} hours",),
    )
    conn.commit()
    return cur.rowcount
