import json

from app import helpers, r2, reddit, reddit_media, search
from app.config import get_settings

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "webp": "image/webp", "gif": "image/gif", "mp4": "video/mp4",
         "mov": "video/quicktime", "webm": "video/webm"}


def _mime(key: str) -> str:
    return _MIME.get(key.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def _fetch_r2(key: str) -> bytes:
    s = get_settings()
    return r2.client().get_object(Bucket=s.r2_bucket, Key=key)["Body"].read()


def _upload(token: str, key: str) -> reddit_media.Asset:
    return reddit_media.upload_asset(
        token, key.rsplit("/", 1)[-1], _mime(key), _fetch_r2(key))


def _post_native(token: str, media: list, title: str) -> str | None:
    """Try a native media post. Returns the post id, None to request the
    self-post fallback, and raises RedditError only after Reddit accepted an
    async submit but the post never showed up in the poll window (no fallback
    then — it may still be transcoding; a retry adopts it by title)."""
    s = get_settings()
    videos = [m for m in media if m["kind"] == "video" and m["thumb_key"]]
    images = [m for m in media if m["kind"] == "image"]
    try:
        if videos:
            v = videos[0]
            video = _upload(token, v["r2_key"])
            poster = reddit_media.upload_asset(
                token, "poster.jpg", "image/jpeg", _fetch_r2(v["thumb_key"]))
            reddit_media.submit_video(token, subreddit=s.subreddit, title=title,
                                      video_url=video.url, poster_url=poster.url,
                                      flair_id=s.sighting_flair_id)
            timeout = reddit_media.VIDEO_POLL_TIMEOUT
        elif len(images) == 1:
            asset = _upload(token, images[0]["r2_key"])
            reddit_media.submit_image(token, subreddit=s.subreddit, title=title,
                                      image_url=asset.url,
                                      flair_id=s.sighting_flair_id)
            timeout = reddit_media.IMAGE_POLL_TIMEOUT
        elif images:
            assets = [_upload(token, m["r2_key"]) for m in images[:20]]
            return reddit_media.submit_gallery(
                token, subreddit=s.subreddit, title=title,
                asset_ids=[a.asset_id for a in assets],
                flair_id=s.sighting_flair_id)
        else:
            return None
    except reddit.RateLimited:
        raise  # a self post would be rate-limited too — let the caller retry
    except Exception as exc:  # lease/upload/submit rejected → self-post fallback
        print(f"native media post failed, falling back to self post: {exc}")
        return None
    post_id = reddit_media.wait_for_post_id(
        token, username=s.script_username, title=title, timeout_s=timeout)
    if not post_id:
        raise reddit.RedditError(
            "media post accepted by Reddit but still processing — retry shortly")
    return post_id


def post_sighting(conn, sighting_id: int, *, verified: bool) -> str:
    """Post a sighting to the subreddit as the bot and mark it live.

    Native media post (video-first, else image/gallery) with the details as a
    first comment; self post when there is no media or the native path fails
    before Reddit accepted the submit. Shared by the verify fast-lane and mod
    approval. Raises reddit.RateLimited / reddit.RedditError without changing
    status so the caller can retry.
    """
    s = get_settings()
    row = conn.execute("SELECT * FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    clean = dict(row)
    for f in ("movement", "sensors", "witness_background"):
        clean[f] = json.loads(row[f]) if row[f] else []
    media = conn.execute(
        "SELECT r2_key, thumb_key, kind FROM media WHERE sighting_id=? ORDER BY sort_order",
        (sighting_id,),
    ).fetchall()
    slug = helpers.slugify(row["title"])
    gallery_url = f"{s.base_url}/sighting/{sighting_id}/{slug}"
    location_line = ", ".join(dict.fromkeys(
        p for p in (row["location_text"], row["city"], row["country"]) if p))
    tag = "verified" if verified else "self-reported"
    attribution = f"Reported by u/{row['reddit_username']} ({tag} via ufosighting.report)"
    title = row["title"]
    token = reddit.script_token()

    post_id = None
    if media:
        # A previous attempt may have posted but timed out on the id poll —
        # adopt that post instead of double-posting.
        post_id = reddit_media.find_recent_post_id(
            token, username=s.script_username, title=title)
        if post_id is None:
            post_id = _post_native(token, media, title)  # may raise; None = fallback
    native = post_id is not None

    body = helpers.format_post_body(
        clean,
        sighted_local=helpers.from_utc(row["sighted_at"], row["tz_name"]),
        location_line=location_line,
        media_urls=[] if native else [r2.public_url(m["r2_key"]) for m in media],
        gallery_url=gallery_url,
        attribution=attribution,
    )
    if native:
        try:
            reddit_media.comment(token, post_id=post_id, text=body)
        except reddit.RedditError as exc:
            print(f"details comment on {post_id} failed (non-fatal): {exc}")
    else:
        post_id = reddit.submit_post(
            token, subreddit=s.subreddit,
            title=title, body=body, flair_id=s.sighting_flair_id,
        )
    conn.execute(
        "UPDATE sightings SET reddit_post_id=?, status='live', username_verified=?, "
        "verify_token=NULL WHERE id=?",
        (post_id, 1 if verified else row["username_verified"], sighting_id),
    )
    conn.commit()
    search.index_sightings(conn, [sighting_id])
    return post_id
