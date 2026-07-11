"""Ingest Sighting-flaired posts from the subreddit into the gallery, extracting
date/time/location via LLM + geocoding. Run by ufosighting-ingest.timer;
`--backfill` walks the last 30 days once."""
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

from app import db, extract, geocode, helpers, r2, reddit
from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
CT_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
BACKFILL_PAGE_SLEEP_SECONDS = 3
PER_POST_SLEEP_SECONDS = 2
BACKFILL_DAYS = 30


def _fetch_image(url: str):
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"User-Agent": get_settings().user_agent})
    if resp.status_code != 200:
        return None
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    return resp.content, ct, CT_EXT.get(ct, ".jpg")


def download_media(post: dict) -> list[tuple[bytes, str, str]]:
    out = []
    url = post.get("url", "") or ""
    gallery = post.get("media_metadata")
    if gallery:
        for item in gallery.values():
            src = (item.get("s", {}) or {}).get("u")
            if item.get("e") == "Image" and src:
                out.append(_fetch_image(src.replace("&amp;", "&")))
    elif url.lower().endswith(IMAGE_EXTS) or "i.redd.it" in url:
        out.append(_fetch_image(url))
    return [m for m in out if m]


def fetch_op_comments(token, post) -> list[str]:
    author = post.get("author")
    if not token or not author:
        return []
    try:
        resp = httpx.get(
            f"https://oauth.reddit.com/comments/{post['id']}",
            params={"depth": 1, "limit": 30, "sort": "top"},
            headers={"Authorization": f"bearer {token}", "User-Agent": get_settings().user_agent},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        listing = resp.json()
        if len(listing) < 2:
            return []
        out = []
        for child in listing[1]["data"]["children"]:
            d = child.get("data", {})
            if d.get("author") == author and d.get("body"):
                out.append(d["body"])
        return out[:10]
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []


def build_sighted_at(clamped: dict, post_created_iso: str) -> tuple[str, str]:
    if not clamped.get("date"):
        return post_created_iso, "UTC"
    tz_name = clamped.get("timezone") or "UTC"
    tm = clamped.get("time") or "12:00"
    try:
        return helpers.to_utc(clamped["date"], tm, tz_name).strftime(ISO), tz_name
    except Exception:
        return post_created_iso, "UTC"


def ingest_post(conn, post: dict, token=None) -> bool:
    pid = post["id"]
    if conn.execute("SELECT 1 FROM sightings WHERE reddit_post_id=?", (pid,)).fetchone():
        return False
    post_created_iso = datetime.fromtimestamp(post.get("created_utc", 0), timezone.utc).strftime(ISO)

    op_comments = fetch_op_comments(token, post)
    text = extract.combine_post_text(post, op_comments)
    clamped = extract.validate_and_clamp(extract.extract_fields(text),
                                         post_created_iso=post_created_iso)

    coords = None
    if clamped.get("location_text"):
        coords = geocode.forward(conn, clamped["location_text"])

    sighted_at, tz_name = build_sighted_at(clamped, post_created_iso)
    title = (post.get("title") or "Untitled sighting")[:300]
    description = (post.get("selftext") or "").strip() or (clamped.get("summary") or "")
    city = (coords or {}).get("city") or clamped.get("city")
    country = (coords or {}).get("country") or clamped.get("country")

    cur = conn.execute(
        """INSERT INTO sightings
             (source, reddit_username, title, description, sighted_at, tz_name,
              shape, num_objects, duration_seconds, location_text, city, country,
              lat, lon, reddit_post_id, status)
           VALUES ('reddit',?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'live')""",
        (post.get("author") or "unknown", title, description, sighted_at, tz_name,
         clamped.get("shape"), clamped.get("num_objects"), clamped.get("duration_seconds"),
         clamped.get("location_text") or "", city, country,
         (coords or {}).get("lat"), (coords or {}).get("lon"), pid),
    )
    sid = cur.lastrowid
    conn.commit()
    try:
        for i, (data, ct, ext) in enumerate(download_media(post)):
            now = datetime.now(timezone.utc)
            key = f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"
            r2.put_bytes(key, data, ct)
            conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) "
                         "VALUES (?,?, 'image', ?)", (sid, key, i))
        conn.commit()
    except Exception as exc:
        print(f"ingest media for {pid} failed: {exc}")
    return True


def ingest_once(conn, *, limit=100, after=None) -> dict:
    s = get_settings()
    token = reddit.script_token()
    posts, _after = reddit.list_flair_posts(token, subreddit=s.ingest_subreddit,
                                            flair="Sighting", limit=limit, after=after)
    added = 0
    for p in posts:
        if ingest_post(conn, p, token=token):
            added += 1
            time.sleep(PER_POST_SLEEP_SECONDS)
    return {"seen": len(posts), "added": added}


def main(backfill: bool = False) -> None:
    conn = db.connect(get_settings().db_path)
    try:
        if backfill:
            cutoff = time.time() - BACKFILL_DAYS * 86400
            after, total, stop = None, 0, False
            while not stop:
                s = get_settings()
                token = reddit.script_token()
                posts, after = reddit.list_flair_posts(token, subreddit=s.ingest_subreddit,
                                                       flair="Sighting", limit=100, after=after)
                if not posts:
                    break
                for p in posts:
                    if p.get("created_utc", 0) < cutoff:
                        stop = True
                        break
                    if ingest_post(conn, p, token=token):
                        total += 1
                        time.sleep(PER_POST_SLEEP_SECONDS)
                if not after:
                    break
                time.sleep(BACKFILL_PAGE_SLEEP_SECONDS)
            print(f"ingest backfill: added={total}")
        else:
            print("ingest:", ingest_once(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main(backfill="--backfill" in sys.argv)
