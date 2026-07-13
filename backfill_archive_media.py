"""Recover missing media from r/UFOs_Archive (SaltyAdminBot re-hosts every
Sighting post's media natively, so it survives original deletion).

Mapping is exact, not fuzzy: we search the archive sub for the sighting's
title, then require the bot's own comment ("**Original Post ID:** xxxxx") to
match our reddit_post_id before touching anything.

    nohup .venv/bin/python backfill_archive_media.py > /tmp/archive_media.log 2>&1 &

Resume-safe: rows that gained media are skipped; tried-but-unmatched ids are
remembered in data/archive_media_tried.json so reruns stay cheap.
"""
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import httpx

import ingest
from app import db, r2, reddit, search
from app.config import get_settings

ARCHIVE_SUB = "UFOs_Archive"
BOT = "SaltyAdminBot"
STATE = "data/archive_media_tried.json"
SLEEP = 2  # per API call — shared script app
ORIG_ID_RE = re.compile(r"Original Post ID:\*{0,2}\s*\**([a-z0-9]{5,9})")


def _headers(tok):
    return {"Authorization": f"bearer {tok}", "User-Agent": get_settings().user_agent}


def find_archive_post(tok, title: str, original_id: str) -> dict | None:
    q = title[:100].replace('"', " ").strip()
    resp = httpx.get(f"https://oauth.reddit.com/r/{ARCHIVE_SUB}/search",
                     params={"q": f'title:"{q}"', "restrict_sr": 1,
                             "sort": "new", "limit": 5, "type": "link"},
                     headers=_headers(tok), timeout=30)
    time.sleep(SLEEP)
    if resp.status_code != 200:
        return None
    for child in resp.json().get("data", {}).get("children", []):
        p = child["data"]
        if p.get("author") != BOT:
            continue
        # verify identity via the bot's own comment
        cr = httpx.get(f"https://oauth.reddit.com/comments/{p['id']}",
                       params={"limit": 8, "depth": 1},
                       headers=_headers(tok), timeout=30)
        time.sleep(SLEEP)
        if cr.status_code != 200:
            continue
        try:
            comments = cr.json()[1]["data"]["children"]
        except (KeyError, IndexError, ValueError):
            continue
        for c in comments:
            body = (c.get("data", {}) or {}).get("body") or ""
            m = ORIG_ID_RE.search(body)
            if m and m.group(1) == original_id:
                return p
    return None


def attach_media(conn, sighting_id: int, archive_post: dict) -> int:
    items = ingest.download_media(archive_post)
    for i, (data, ct, ext) in enumerate(items):
        now = datetime.now(timezone.utc)
        key = f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"
        r2.put_bytes(key, data, ct)
        kind = "video" if ct.startswith("video/") else "image"
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) "
                     "VALUES (?,?,?,?)", (sighting_id, key, kind, i))
    conn.commit()
    if items:
        search.index_sightings(conn, [sighting_id])
    return len(items)


def main() -> None:
    conn = db.connect(get_settings().db_path)
    tried = set()
    if os.path.exists(STATE):
        tried = set(json.load(open(STATE)))
    rows = conn.execute(
        """SELECT id, title, reddit_post_id FROM sightings s
           WHERE source='reddit' AND reddit_post_id IS NOT NULL
             AND status IN ('live','deleted_by_user','removed_on_reddit')
             AND NOT EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id)
             AND NOT EXISTS (SELECT 1 FROM yt_jobs y WHERE y.sighting_id = s.id
                             AND y.status != 'failed')
           ORDER BY sighted_at DESC""").fetchall()
    todo = [r for r in rows if r["id"] not in tried]
    print(f"{len(rows)} media-less sightings, {len(todo)} to try", flush=True)
    tok = reddit.read_token()
    recovered = files = 0
    for n, r in enumerate(todo, 1):
        try:
            hit = find_archive_post(tok, r["title"], r["reddit_post_id"])
            if hit:
                added = attach_media(conn, r["id"], hit)
                if added:
                    recovered += 1
                    files += added
                    print(f"RECOVERED sighting {r['id']} ({r['reddit_post_id']}) "
                          f"from archive {hit['id']}: {added} file(s)", flush=True)
        except reddit.RedditError as exc:
            print(f"token refresh needed: {exc}", flush=True)
            tok = reddit.read_token()
        except Exception as exc:
            print(f"sighting {r['id']} failed: {exc}", flush=True)
        tried.add(r["id"])
        if n % 25 == 0:
            json.dump(sorted(tried), open(STATE, "w"))
            print(f"progress: {n}/{len(todo)} tried, {recovered} recovered", flush=True)
    json.dump(sorted(tried), open(STATE, "w"))
    print(f"done: recovered media for {recovered} sightings ({files} files) "
          f"of {len(todo)} tried", flush=True)


if __name__ == "__main__":
    main()
