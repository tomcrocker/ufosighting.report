"""Best-effort DMs to reporters from the bot: sighting approved / rejected.

Two hard rules:
- Never fatal. A failed DM must not break posting or an admin action, so every
  send is wrapped and only logged on failure.
- Only message a *confirmed* account. If the reporter never clicked the verify
  link we don't know the username is really theirs, so we stay silent rather
  than DM a stranger.
"""
from app import reddit


def _send(username: str, subject: str, text: str) -> bool:
    try:
        reddit.send_message(reddit.script_token(), to=username, subject=subject, text=text)
        print(f"notify: DM sent to u/{username} ({subject!r})")
        return True
    except Exception as exc:  # noqa: BLE001 — DMs are never fatal
        print(f"notify: DM to u/{username} failed: {exc}")
        return False


def _verified_reporter(conn, sighting_id: int):
    row = conn.execute(
        "SELECT reddit_username, username_verified FROM sightings WHERE id=?",
        (sighting_id,)).fetchone()
    if row and row["username_verified"] and row["reddit_username"]:
        return row["reddit_username"]
    return None


def approval_dm(conn, sighting_id: int, post_id: str) -> bool:
    """Tell a verified reporter their sighting is live, with the Reddit link."""
    user = _verified_reporter(conn, sighting_id)
    if not user or not post_id:
        return False
    url = f"https://www.reddit.com/comments/{post_id}"
    text = (f"Hi u/{user},\n\n"
            f"Thanks for confirming — your sighting is now live on r/UFOs:\n\n{url}\n\n"
            f"It's also archived at ufosighting.report. Thanks for contributing to the "
            f"community record.")
    return _send(user, "Your UFO sighting is live on r/UFOs", text)


def rejection_dm(conn, sighting_id: int, reason: str) -> bool:
    """Tell a verified reporter their submission wasn't posted, and why. No DM
    when there's no reason to give, or the account was never confirmed."""
    reason = (reason or "").strip()
    if not reason:
        return False
    user = _verified_reporter(conn, sighting_id)
    if not user:
        return False
    text = (f"Hi u/{user},\n\n"
            f"Your recent sighting submitted through ufosighting.report was not posted "
            f"to r/UFOs.\n\n"
            f"Reason: {reason}\n\n"
            f"You're welcome to submit again if you can address this. Thanks for "
            f"understanding.")
    return _send(user, "About your UFO sighting submission", text)
