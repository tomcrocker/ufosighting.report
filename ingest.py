"""Ingest Sighting-flaired posts from the subreddit into the gallery.
Run by ufosighting-ingest.timer; `--backfill` walks history once."""
import sys
import uuid
from datetime import datetime, timezone

import httpx

from app import db, r2, reddit
from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
CT_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


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


def ingest_post(conn, post: dict) -> bool:
    pid = post["id"]
    if conn.execute("SELECT 1 FROM sightings WHERE reddit_post_id=?", (pid,)).fetchone():
        return False
    sighted_at = datetime.fromtimestamp(post.get("created_utc", 0), timezone.utc).strftime(ISO)
    title = (post.get("title") or "Untitled sighting")[:300]
    cur = conn.execute(
        """INSERT INTO sightings (source, reddit_username, title, description, sighted_at,
             tz_name, location_text, reddit_post_id, status)
           VALUES ('reddit', ?, ?, ?, ?, 'UTC', '', ?, 'live')""",
        (post.get("author") or "unknown", title, post.get("selftext") or "", sighted_at, pid),
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
    posts, _after = reddit.list_flair_posts(token, subreddit=s.subreddit,
                                            flair="Sighting", limit=limit, after=after)
    added = sum(1 for p in posts if ingest_post(conn, p))
    return {"seen": len(posts), "added": added}


def main(backfill: bool = False) -> None:
    conn = db.connect(get_settings().db_path)
    try:
        if backfill:
            after, total = None, 0
            while True:
                s = get_settings()
                token = reddit.script_token()
                posts, after = reddit.list_flair_posts(token, subreddit=s.subreddit,
                                                       flair="Sighting", limit=100, after=after)
                if not posts:
                    break
                total += sum(1 for p in posts if ingest_post(conn, p))
                if not after:
                    break
            print(f"ingest backfill: added={total}")
        else:
            print("ingest:", ingest_once(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main(backfill="--backfill" in sys.argv)
