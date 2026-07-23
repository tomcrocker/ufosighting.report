"""Best-effort Discord notifications via an incoming webhook.

Fires on new sighting submissions so the mod team sees them in a channel.
Never fatal and never blocks a submission: a missing URL or a webhook failure
just logs and returns. The webhook URL is a secret (anyone with it can post to
the channel), so it lives in the env, not the repo.
"""
import httpx

from app import helpers
from app.config import get_settings

BRAND_GREEN = 0x6EE7A0


def _post(payload: dict) -> bool:
    url = get_settings().discord_webhook_url
    if not url:
        return False
    try:
        r = httpx.post(url, json=payload, timeout=10)
        if r.status_code >= 300:
            print(f"discord: webhook HTTP {r.status_code}")
            return False
        return True
    except httpx.HTTPError as exc:
        print(f"discord: webhook failed: {exc}")
        return False


def notify_new_sighting(conn, sighting_id: int) -> bool:
    """Post an embed for a freshly-submitted sighting (status pending_verify)."""
    if not get_settings().discord_webhook_url:
        return False
    row = conn.execute("SELECT * FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    if row is None:
        return False
    media = conn.execute("SELECT kind FROM media WHERE sighting_id=?", (sighting_id,)).fetchall()
    imgs = sum(1 for m in media if m["kind"] == "image")
    vids = sum(1 for m in media if m["kind"] == "video")
    media_str = ", ".join(p for p in (
        f"{imgs} photo{'s' if imgs != 1 else ''}" if imgs else "",
        f"{vids} video{'s' if vids != 1 else ''}" if vids else "") if p) or "none"
    try:
        when = helpers.sighting_time_display(row["sighted_at"], row["tz_name"]) or row["sighted_at"]
    except Exception:  # noqa: BLE001
        when = row["sighted_at"]
    kind = "First-hand" if row["first_hand"] else "Shared (second-hand)"
    s = get_settings()
    embed = {
        "title": "🛸 New sighting submitted",
        "description": (row["title"] or "")[:250],
        "url": f"{s.base_url}/sighting/{sighting_id}",
        "color": BRAND_GREEN,
        "fields": [
            {"name": "Reporter", "value": f"u/{row['reddit_username']}", "inline": True},
            {"name": "Type", "value": kind, "inline": True},
            {"name": "Media", "value": media_str, "inline": True},
            {"name": "Location", "value": (row["location_text"] or "unknown")[:200], "inline": True},
            {"name": "When", "value": (when or "—")[:100], "inline": True},
            {"name": "Status", "value": "Awaiting verification", "inline": True},
        ],
        "footer": {"text": "ufosighting.report — the reporter has not confirmed their account yet"},
    }
    return _post({"embeds": [embed]})
