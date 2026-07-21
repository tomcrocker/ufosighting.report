"""One-shot: backfill sightings.bsky_uri from the bot's Bluesky feed, matching
each post to a sighting via the /sighting/<id> link in its text. This lets us
retract posts whose sighting later gets removed. Idempotent (only fills NULLs)."""
import re

import httpx

from app import db
from app.config import get_settings

FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"


def fetch_posts(handle):
    posts, cursor = [], None
    for _ in range(30):
        params = {"actor": handle, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = httpx.get(FEED, params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        for item in d.get("feed", []):
            p = item["post"]
            if p.get("author", {}).get("handle") != handle:  # skip any reposts
                continue
            posts.append((p["uri"], p.get("record", {}).get("text", "")))
        cursor = d.get("cursor")
        if not cursor:
            break
    return posts


def main():
    s = get_settings()
    conn = db.connect(s.db_path)
    posts = fetch_posts(s.bsky_handle)
    n = 0
    for uri, text in posts:
        m = re.search(r"/sighting/(\d+)", text)
        if not m:
            continue
        n += conn.execute("UPDATE sightings SET bsky_uri=? WHERE id=? AND bsky_uri IS NULL",
                          (uri, int(m.group(1)))).rowcount
    conn.commit()
    print(f"fetched {len(posts)} posts, backfilled bsky_uri for {n} sightings")


if __name__ == "__main__":
    main()
