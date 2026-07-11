import json

from app import helpers, r2, reddit
from app.config import get_settings


def post_sighting(conn, sighting_id: int, *, verified: bool) -> str:
    """Post a sighting to the subreddit as the bot and mark it live.

    Shared by the verify fast-lane and mod approval. Raises reddit.RateLimited
    / reddit.RedditError without changing status so the caller can retry.
    """
    s = get_settings()
    row = conn.execute("SELECT * FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    clean = dict(row)
    for f in ("movement", "sensors", "witness_background"):
        clean[f] = json.loads(row[f]) if row[f] else []
    media = conn.execute(
        "SELECT r2_key FROM media WHERE sighting_id=? ORDER BY sort_order", (sighting_id,)
    ).fetchall()
    slug = helpers.slugify(row["title"])
    gallery_url = f"{s.base_url}/sighting/{sighting_id}/{slug}"
    location_line = ", ".join(dict.fromkeys(
        p for p in (row["location_text"], row["city"], row["country"]) if p))
    tag = "verified" if verified else "self-reported"
    attribution = f"Reported by u/{row['reddit_username']} ({tag} via ufosighting.report)"
    body = helpers.format_post_body(
        clean,
        sighted_local=helpers.from_utc(row["sighted_at"], row["tz_name"]),
        location_line=location_line,
        media_urls=[r2.public_url(m["r2_key"]) for m in media],
        gallery_url=gallery_url,
        attribution=attribution,
    )
    post_id = reddit.submit_post(
        reddit.script_token(), subreddit=s.subreddit,
        title=row["title"], body=body, flair_id=s.sighting_flair_id,
    )
    conn.execute(
        "UPDATE sightings SET reddit_post_id=?, status='live', username_verified=?, "
        "verify_token=NULL WHERE id=?",
        (post_id, 1 if verified else row["username_verified"], sighting_id),
    )
    conn.commit()
    return post_id
