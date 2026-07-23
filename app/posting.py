import json

from app import appsettings, helpers, mediameta, r2, reddit, reddit_media, search, skycontext
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


def _reddit_safe_key(m) -> str:
    """Always hand Reddit the JPEG display derivative when we have one.

    It rejects HEIC/HEIF outright, but the subtler problem is modern phone
    JPEGs: a Galaxy S25+ Ultra HDR file carries a gain map that Reddit's
    pipeline mis-applies and renders as a solid black gallery image. The
    derivative is a plain re-encode with the gain map and EXIF stripped, and
    Reddit re-compresses everything anyway, so there's nothing to lose. The
    site keeps serving the untouched original for download.
    """
    return m["display_key"] or m["r2_key"]


def select_post_media(media: list, prefer: str | None = None) -> tuple[str, list]:
    """Decide what the Reddit post itself carries.

    A Reddit post is single-medium: one video, one image, or an images-only
    gallery. Galleries reject video, so when a reporter uploads both something
    has to sit out. `prefer` is their choice ('video' | 'images'); without one
    the video leads, since motion and duration carry more than a still.

    Returns (kind, files). Whatever isn't selected is listed in the pinned
    comment and kept on the archive page — it must never just disappear.
    """
    videos = [m for m in media if m["kind"] == "video" and m["thumb_key"]]
    images = [m for m in media if m["kind"] == "image"]
    if videos and images and prefer == "images":
        videos = []  # reporter asked for the photos to lead
    if videos:
        return "video", [videos[0]]
    if len(images) == 1:
        return "image", images[:1]
    if images:
        return "gallery", images[:20]
    return "none", []


def _post_native(token: str, media: list, title: str,
                 prefer: str | None = None) -> str | None:
    """Try a native media post. Returns the post id, None to request the
    self-post fallback, and raises RedditError only after Reddit accepted an
    async submit but the post never showed up in the poll window (no fallback
    then — it may still be transcoding; a retry adopts it by title)."""
    s = get_settings()
    kind, chosen = select_post_media(media, prefer)
    images = chosen if kind in ("image", "gallery") else []
    try:
        if kind == "video":
            v = chosen[0]
            video = _upload(token, v["r2_key"])
            poster = reddit_media.upload_asset(
                token, "poster.jpg", "image/jpeg", _fetch_r2(v["thumb_key"]))
            reddit_media.submit_video(token, subreddit=s.subreddit, title=title,
                                      video_url=video.url, poster_url=poster.url,
                                      flair_id=s.sighting_flair_id)
            timeout = reddit_media.VIDEO_POLL_TIMEOUT
        elif kind == "image":
            asset = _upload(token, _reddit_safe_key(images[0]))
            reddit_media.submit_image(token, subreddit=s.subreddit, title=title,
                                      image_url=asset.url,
                                      flair_id=s.sighting_flair_id)
            timeout = reddit_media.IMAGE_POLL_TIMEOUT
        elif kind == "gallery":
            assets = [_upload(token, _reddit_safe_key(m)) for m in images]
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


# How long to wait for media processing before posting anyway. A stuck or
# failed thumbnail must never strand a sighting in the queue forever.
POST_QUEUE_TIMEOUT_MINUTES = 10
MAX_POST_ATTEMPTS = 5


def process_post_queue(conn, limit: int = 1) -> int:
    """Post sightings the verify click queued, once their media is ready.

    "Ready" means every attached file has a thumbnail — for a video that's the
    poster frame Reddit needs, and the thing whose absence used to make
    _post_native silently drop the video. Falls through after the timeout so a
    failed thumbnail delays a post instead of losing it.

    While the moderation hold is on, queued sightings are diverted into the
    review queue instead of posting, so nothing reaches r/UFOs without a
    moderator's approval.
    """
    if appsettings.hold_posts(conn):
        held = conn.execute(
            """UPDATE sightings SET status='pending_review'
                 WHERE status='pending_post' AND pending_post_at IS NOT NULL"""
        ).rowcount
        conn.commit()
        if held:
            print(f"post-queue: moderation hold ON — {held} sighting(s) diverted to review")
        return 0
    rows = conn.execute(
        """SELECT id, username_verified, title FROM sightings
             WHERE status='pending_post'
               -- only rows the verify flow queued: a pending_post row with no
               -- queue timestamp predates this flow and must never auto-post
               AND pending_post_at IS NOT NULL
               AND post_attempts < ?
               AND (NOT EXISTS (SELECT 1 FROM media m
                                 WHERE m.sighting_id = sightings.id
                                   AND m.thumb_key IS NULL)
                    OR pending_post_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?))
             ORDER BY pending_post_at
             LIMIT ?""",
        (MAX_POST_ATTEMPTS, f"-{POST_QUEUE_TIMEOUT_MINUTES} minutes", limit),
    ).fetchall()
    done = 0
    for r in rows:
        # count the attempt before trying, so a call that dies mid-flight can't
        # spin forever against Reddit
        conn.execute("UPDATE sightings SET post_attempts = post_attempts + 1 WHERE id=?",
                     (r["id"],))
        conn.commit()
        try:
            post_sighting(conn, r["id"], verified=bool(r["username_verified"]))
            print(f"post-queue: posted sighting {r['id']}")
            done += 1
            try:  # brand-new URL — nudge IndexNow (best-effort)
                from app import indexnow
                slug = helpers.slugify(r["title"])
                indexnow.submit_url(
                    f"{get_settings().base_url}/sighting/{r['id']}/{slug}")
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 — stays queued for the next pass
            print(f"post-queue: sighting {r['id']} failed, will retry: {exc}")
    return done


def details_body(conn, sighting_id: int, *, verified: bool, native: bool,
                 sats: dict | None = None) -> str:
    """Assemble the details block (pinned comment on native posts, the body
    itself on self posts).

    Shared by the initial post and by the sky worker's later edit, so both
    render identically — the only difference is `sats`, which doesn't exist
    until after the post goes live.
    """
    s = get_settings()
    row = conn.execute("SELECT * FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    clean = dict(row)
    for f in ("movement", "sensors", "witness_background"):
        clean[f] = json.loads(row[f]) if row[f] else []
    media = conn.execute(
        "SELECT r2_key, kind, thumb_key, exif_json FROM media "
        "WHERE sighting_id=? ORDER BY sort_order",
        (sighting_id,),
    ).fetchall()
    provenance = None  # flag the primary file if it doesn't look like an original
    if media and media[0]["exif_json"]:
        provenance = mediameta.provenance(json.loads(media[0]["exif_json"]))
    gallery_url = f"{s.base_url}/sighting/{sighting_id}/{helpers.slugify(row['title'])}"
    location_line = ", ".join(dict.fromkeys(
        p for p in (row["location_text"], row["city"], row["country"]) if p))
    sky = skycontext.markdown(
        skycontext.links(row["lat"], row["lon"], row["sighted_at"]), sats)
    details = helpers.format_post_body(
        clean,
        sighted_local=helpers.from_utc(row["sighted_at"], row["tz_name"]),
        location_line=location_line,
        media_urls=[] if native else [r2.public_url(m["r2_key"]) for m in media],
        media_provenance=provenance,
        sky=sky,
    )
    blocks = [_attribution_header(row, verified=verified, gallery_url=gallery_url)]
    if native:
        # Reddit carries one medium; say out loud what it couldn't carry, so
        # the thread never quietly hides evidence the reporter submitted.
        left_out = _left_out_line(media, row["primary_media"])
        if left_out:
            blocks.append(left_out)
    blocks += ["---", details]
    return "\n\n".join(blocks)


def _left_out_line(media: list, prefer: str | None) -> str:
    """Name the files the Reddit post couldn't include, with direct links."""
    _, chosen = select_post_media(media, prefer)
    taken = {m["r2_key"] for m in chosen}
    rest = [m for m in media if m["r2_key"] not in taken]
    if not rest:
        return ""
    vids = [m for m in rest if m["kind"] == "video"]
    imgs = [m for m in rest if m["kind"] == "image"]
    bits = []
    for group, noun in ((vids, "video"), (imgs, "photo")):
        if group:
            links = ", ".join(f"[{i}]({r2.public_url(m['r2_key'])})"
                              for i, m in enumerate(group, 1))
            bits.append(f"{len(group)} {noun}{'s' if len(group) > 1 else ''} ({links})")
    return (f"📎 **Also submitted:** {' and '.join(bits)}. A Reddit post can only carry "
            f"one video *or* photos, never both, so the rest lives on the archive page "
            f"linked above.")


def _attribution_header(row, *, verified: bool, gallery_url: str) -> str:
    """Who filed this and how we know, stated up front.

    Readers land on a post from an account that isn't the witness, so the
    comment has to answer "who actually saw this, and why should I believe the
    name?" before it shows any sighting details.
    """
    s = get_settings()
    user, bot = row["reddit_username"], s.script_username
    tag = "verified" if verified else "self-reported"
    if row["first_hand"]:
        who = f"**Reported by u/{user}** ({tag} via ufosighting.report)"
    else:
        src = f" Reported source: {row['source_note']}." if row["source_note"] else ""
        who = (f"⚠️ **Shared by u/{user}** ({tag} account, via ufosighting.report). "
               f"This is not their own sighting.{src}")
    if verified:
        proof = (f"u/{user} confirmed this submission from their own Reddit account "
                 f"through a one-time link, so the name above is verified as the "
                 f"submitter.")
    else:
        proof = (f"u/{user} never confirmed the submission, so the name above is "
                 f"self-reported and unverified.")
    return "\n\n".join([
        who,
        f"*This is an automated post.* The report was filed through the structured "
        f"sighting form on [ufosighting.report](https://ufosighting.report) and posted "
        f"here by u/{bot} on the reporter's behalf. {proof}",
        f"📎 **[Original-quality media and full report]({gallery_url})**. Reddit "
        f"re-encodes every upload, so the archive keeps the untouched original files "
        f"alongside the structured details and the map entry.",
    ])


def refresh_sky_comment(conn, sighting_id: int) -> bool:
    """Fold freshly-computed satellite passes into the already-posted pinned
    comment. Best-effort: returns False when there's nothing to edit."""
    row = conn.execute(
        "SELECT bot_comment_id, sky_events, username_verified FROM sightings WHERE id=?",
        (sighting_id,)).fetchone()
    if row is None or not row["bot_comment_id"] or not row["sky_events"]:
        return False
    sats = json.loads(row["sky_events"])
    if not sats.get("checked"):
        return False  # nothing computed worth showing
    body = details_body(conn, sighting_id,
                        verified=bool(row["username_verified"]), native=True, sats=sats)
    reddit_media.edit_comment(reddit.script_token(),
                              comment_id=row["bot_comment_id"], text=body)
    return True


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
    media = conn.execute(
        "SELECT r2_key, thumb_key, display_key, kind, exif_json FROM media "
        "WHERE sighting_id=? ORDER BY sort_order", (sighting_id,),
    ).fetchall()
    title = row["title"]
    token = reddit.script_token()

    post_id = None
    if media:
        # A previous attempt may have posted but timed out on the id poll —
        # adopt that post instead of double-posting.
        post_id = reddit_media.find_recent_post_id(
            token, username=s.script_username, title=title)
        if post_id is None:
            # may raise; None = fallback. primary_media is the reporter's pick
            # when they uploaded both a video and photos.
            post_id = _post_native(token, media, title, row["primary_media"])
    native = post_id is not None

    # sky_events is still NULL here (the row isn't live yet, and only live rows
    # are picked up), so the comment ships with the investigation links and the
    # worker edits the computed passes in afterwards via refresh_sky_comment.
    body = details_body(conn, sighting_id, verified=verified, native=native)
    if native:
        try:
            comment_id = reddit_media.comment(token, post_id=post_id, text=body)
            if comment_id:
                conn.execute("UPDATE sightings SET bot_comment_id=? WHERE id=?",
                             (comment_id, sighting_id))
                conn.commit()
                # pin the details to the top of the thread (bot is a mod)
                reddit_media.pin_comment(token, comment_id=comment_id)
                # preemptive approve: marks the comment mod-approved so the
                # spam filter leaves it alone (no-op if it wasn't removed)
                try:
                    reddit.approve(token, comment_id=comment_id)
                except reddit.RedditError:
                    reddit.approve(reddit.read_token(), comment_id=comment_id)
        except reddit.RedditError as exc:
            print(f"details comment/pin on {post_id} failed (non-fatal): {exc}")
    else:
        post_id = reddit.submit_post(
            token, subreddit=s.subreddit,
            title=title, body=body, flair_id=s.sighting_flair_id,
        )
    # The sitewide spam filter removes the young bot account's posts (media
    # AND plain self posts). Rescue via mod-approve: the bot first, then the
    # personal mod account (the bot may lack the "posts" mod permission or
    # have lost app access). Best-effort, non-fatal.
    info = None
    try:
        info = reddit.fetch_post(token, post_id)
        if info and info.get("removed_by_category") == "reddit":
            try:
                reddit.approve(token, post_id=post_id)
            except reddit.RedditError:
                reddit.approve(reddit.read_token(), post_id=post_id)
            print(f"self-approved spam-filtered post {post_id}")
    except Exception as exc:  # rescue is best-effort — never break the posting
        print(f"self-approve check on {post_id} failed (non-fatal): {exc}")
    # created_utc rides along in the same fetch_post response; the post was just
    # created, so it's authoritative for reddit_posted_at.
    posted_at = helpers.iso_from_epoch(info.get("created_utc")) if info else None
    conn.execute(
        "UPDATE sightings SET reddit_post_id=?, reddit_posted_at=?, status='live', "
        "username_verified=?, verify_token=NULL WHERE id=?",
        (post_id, posted_at, 1 if verified else row["username_verified"], sighting_id),
    )
    conn.commit()
    search.index_sightings(conn, [sighting_id])
    return post_id
