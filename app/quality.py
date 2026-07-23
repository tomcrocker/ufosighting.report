"""A CQS-proxy gate for the reporter's Reddit account.

Reddit filters low-Contributor-Quality-Score authors' posts into the modqueue.
When someone submits through our form, though, the *bot* is the author, so a
trusted bot launders a low-CQS user straight past that filter. Reddit doesn't
expose CQS, so we rebuild the gate from the signals it's derived from — account
age and karma — and route accounts that fail into our own review queue instead
of auto-posting. This is the equivalent of Reddit sending them to modqueue.

Thresholds are deliberately loose: the goal is throwaways and brand-new
accounts, not to wall out ordinary participants.
"""
import time

from app import reddit
from app.config import get_settings


def gate(username: str) -> tuple[bool, str]:
    """(ok, reason). ok=True → safe to auto-post. ok=False → send to review,
    with a human-readable reason for the moderator.

    Fails OPEN on a transient Reddit/API error: a blip on our side must not dump
    every legitimate reporter into the queue. It fails CLOSED only on definite
    signals — a ban, or an account that can't be read at all (suspended /
    shadowbanned / deleted).
    """
    s = get_settings()

    # Ban comes first: posting a banned user's report on their behalf is ban
    # evasion, the worst outcome, and independent of account quality.
    try:
        if reddit.is_banned(username):
            return False, f"🚫 BANNED on r/{s.subreddit} — do not approve (ban evasion)"
    except reddit.RedditError as exc:
        print(f"quality: ban check failed for u/{username}, continuing: {exc}")

    try:
        about = reddit.user_about(username)
    except reddit.RedditError as exc:
        print(f"quality: gate check failed for u/{username}, allowing: {exc}")
        return True, ""
    if about is None:
        return False, "account not found (suspended, shadowbanned, or deleted)"
    if about.get("is_suspended"):
        return False, "account is suspended"

    created = about.get("created_utc")
    if created:
        age_days = (time.time() - created) / 86400
        if age_days < s.cqs_min_account_age_days:
            return False, f"new account ({int(age_days)} days old)"

    link = about.get("link_karma", 0) or 0
    comment = about.get("comment_karma", 0) or 0
    total = about.get("total_karma") or (link + comment)
    # Negative karma is a strong stand-alone signal — legitimate accounts almost
    # never carry it, downvoted trolls do.
    if link < 0 or comment < 0:
        return False, f"negative karma (post {link}, comment {comment})"
    if link < s.cqs_min_link_karma:
        return False, f"low post karma ({link})"
    if comment < s.cqs_min_comment_karma:
        return False, f"low comment karma ({comment})"
    if total < s.cqs_min_karma:
        return False, f"low total karma ({total})"

    if s.cqs_require_verified_email and not about.get("has_verified_email"):
        return False, "email not verified"

    return True, ""
