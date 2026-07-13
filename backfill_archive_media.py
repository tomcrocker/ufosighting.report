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
SLEEP = 1  # per API call — plenty; headroom is ~800 req/10min
_client = httpx.Client(timeout=30)
ORIG_ID_RE = re.compile(r"Original Post ID:\*{0,2}\s*\**([a-z0-9]{5,9})")
ORIG_TEXT_RE = re.compile(
    r"Original post text:\*{0,2}\s*(.+?)(?:\n+\*\*Original Flair|\Z)", re.S)


def _headers(tok):
    return {"Authorization": f"bearer {tok}", "User-Agent": get_settings().user_agent}


def _word_cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def find_archive_post(tok, title: str, original_id: str) -> dict | None:
    clean = title.replace('"', " ").strip()
    # Reddit's phrase search is finicky about length and mid-word cuts —
    # try a couple of shapes before giving up
    queries = [f'title:"{_word_cut(clean, 60)}"',
               f'title:"{_word_cut(clean, 35)}"',
               " ".join(clean.split()[:6])]
    candidates = []
    seen_q = set()
    for q in queries:
        if q in seen_q or len(q) < 8:
            continue
        seen_q.add(q)
        resp = _client.get(f"https://oauth.reddit.com/r/{ARCHIVE_SUB}/search",
                         params={"q": q, "restrict_sr": 1,
                                 "sort": "new", "limit": 5, "type": "link"},
                         headers=_headers(tok), timeout=30)
        time.sleep(SLEEP)
        if resp.status_code != 200:
            continue
        candidates = resp.json().get("data", {}).get("children", [])
        if candidates:
            break
    for child in candidates:
        p = child["data"]
        if p.get("author") != BOT:
            continue
        # verify identity via the bot's own comment
        cr = _client.get(f"https://oauth.reddit.com/comments/{p['id']}",
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
                tm = ORIG_TEXT_RE.search(body)
                return p, (tm.group(1).strip() if tm else "")
    return None


def fetch_original(tok, orig_id: str, author: str) -> tuple[dict | None, list[str]]:
    """The original post's thread stays reachable after deletion/removal:
    the post object keeps created_utc (fallback-time detection) and the OP's
    comments usually survive — often the only place 'Time:/Location:' lives,
    posted after our ingest snapshot."""
    r = _client.get(f"https://oauth.reddit.com/comments/{orig_id}",
                    params={"limit": 100, "depth": 2, "sort": "old"},
                    headers=_headers(tok), timeout=30)
    time.sleep(SLEEP)
    if r.status_code != 200:
        return None, []
    try:
        post = r.json()[0]["data"]["children"][0]["data"]
        top = r.json()[1]["data"]["children"]
    except (KeyError, IndexError, ValueError):
        return None, []
    op: list[str] = []

    def walk(children):
        for c in children:
            d = c.get("data") or {}
            if c.get("kind") != "t1":
                continue
            body = (d.get("body") or "").strip()
            if d.get("author") == author and body and body not in ("[deleted]", "[removed]"):
                op.append(body)
            replies = d.get("replies")
            if isinstance(replies, dict):
                walk(replies.get("data", {}).get("children", []))

    walk(top)
    return post, op[:5]


def repair_from_text(conn, row, text: str) -> bool:
    """The bot's comment preserves the ORIGINAL post body — often the only
    surviving copy of the 'Time:/Location:' lines for fast-deleted posts.
    Re-runs extraction + geocoding; only fills gaps, never overwrites."""
    from datetime import datetime as _dt

    from app import extract, geocode, helpers
    if len(text) < 30 or text.lower() in ("[deleted]", "[removed]"):
        return False
    clamped = extract.validate_and_clamp(
        extract.extract_fields(extract.combine_post_text(
            {"title": row["title"], "selftext": text}, [])),
        post_created_iso=row["created_at"])
    coords = None
    for q in geocode.candidates(clamped.get("location_text") or "",
                                clamped.get("city"), clamped.get("country")):
        coords = geocode.forward(conn, q)
        if coords:
            break
    changed = False
    if row["lat"] is None and (coords or clamped.get("location_text")):
        conn.execute(
            """UPDATE sightings SET location_text=COALESCE(NULLIF(?,''), location_text),
                 city=COALESCE(?, city), country=COALESCE(?, country),
                 lat=?, lon=? WHERE id=?""",
            (clamped.get("location_text") or "",
             (coords or {}).get("city") or clamped.get("city"),
             (coords or {}).get("country") or clamped.get("country"),
             (coords or {}).get("lat"), (coords or {}).get("lon"), row["id"]))
        changed = True
    if clamped.get("date") and not row["description"]:
        sighted_at, tz_name = __import__("ingest").build_sighted_at(
            clamped, row["created_at"])
        conn.execute("UPDATE sightings SET sighted_at=?, tz_name=? WHERE id=?",
                     (sighted_at, tz_name, row["id"]))
        changed = True
    if not (row["description"] or "").strip():
        conn.execute("UPDATE sightings SET description=? WHERE id=?",
                     (text[:8000], row["id"]))
        changed = True
    conn.commit()
    if changed:
        search.index_sightings(conn, [row["id"]])
    return changed


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


def repair_media_row(conn, row, tok) -> list[str]:
    """Media-having rows: fill missing geo/description/sighting-time from the
    original thread (late OP comments!) with the archive comment as body
    fallback. Returns the fixes applied."""
    from datetime import datetime as _dt

    from app import extract, geocode, search as _search
    post, op_comments = fetch_original(tok, row["reddit_post_id"],
                                       row["reddit_username"])
    body = ((post or {}).get("selftext") or "").strip()
    if body in ("[deleted]", "[removed]"):
        body = ""
    if not body and row["lat"] is None:
        hit = find_archive_post(tok, row["title"], row["reddit_post_id"])
        if hit:
            body = hit[1]
    if not body and not op_comments:
        return []

    created_iso = None
    if post and post.get("created_utc"):
        created_iso = _dt.fromtimestamp(
            post["created_utc"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    anchor = created_iso or row["sighted_at"]
    clamped = extract.validate_and_clamp(
        extract.extract_fields(extract.combine_post_text(
            {"title": row["title"], "selftext": body}, op_comments)),
        post_created_iso=anchor)

    fixes = []
    if row["lat"] is None:
        coords = None
        for q in geocode.candidates(clamped.get("location_text") or "",
                                    clamped.get("city"), clamped.get("country")):
            coords = geocode.forward(conn, q)
            if coords:
                break
        if coords or clamped.get("location_text"):
            conn.execute(
                """UPDATE sightings SET location_text=COALESCE(NULLIF(?,''), location_text),
                     city=COALESCE(?, city), country=COALESCE(?, country),
                     lat=?, lon=? WHERE id=?""",
                (clamped.get("location_text") or "",
                 (coords or {}).get("city") or clamped.get("city"),
                 (coords or {}).get("country") or clamped.get("country"),
                 (coords or {}).get("lat"), (coords or {}).get("lon"), row["id"]))
            if coords:
                fixes.append("geo")
    # fallback signature: sighted_at == the post's submission time
    is_fallback = created_iso is not None and abs(
        (_dt.strptime(row["sighted_at"], "%Y-%m-%dT%H:%M:%SZ")
         - _dt.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ")).total_seconds()) <= 120
    if is_fallback and clamped.get("date"):
        sighted_at, tz_name = ingest.build_sighted_at(clamped, created_iso)
        if sighted_at != row["sighted_at"]:
            conn.execute("UPDATE sightings SET sighted_at=?, tz_name=? WHERE id=?",
                         (sighted_at, tz_name, row["id"]))
            fixes.append("time")
    if not (row["description"] or "").strip() and body:
        conn.execute("UPDATE sightings SET description=? WHERE id=?",
                     (body[:8000], row["id"]))
        fixes.append("desc")
    if fixes:
        if "geo" in fixes or "time" in fixes:
            conn.execute("UPDATE sightings SET sky_events=NULL WHERE id=?",
                         (row["id"],))
        conn.commit()
        _search.index_sightings(conn, [row["id"]])
    return fixes


def main_media_rows() -> None:
    conn = db.connect(get_settings().db_path)
    state = "data/archive_media_tried_media.json"
    tried = set(json.load(open(state))) if os.path.exists(state) else set()
    rows = conn.execute(
        """SELECT id, title, reddit_post_id, reddit_username, description,
                  lat, sighted_at
           FROM sightings s
           WHERE source='reddit' AND reddit_post_id IS NOT NULL
             AND status IN ('live','deleted_by_user','removed_on_reddit')
             AND EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id)
             AND (s.lat IS NULL OR TRIM(COALESCE(s.description,'')) = '')
           ORDER BY sighted_at DESC""").fetchall()
    todo = [r for r in rows if r["id"] not in tried]
    print(f"{len(rows)} media rows with gaps, {len(todo)} to try", flush=True)
    tok = reddit.read_token()
    repaired = 0
    for n, r in enumerate(todo, 1):
        try:
            fixes = repair_media_row(conn, r, tok)
            if fixes:
                repaired += 1
                print(f"REPAIRED sighting {r['id']} ({r['reddit_post_id']}): "
                      f"{'+'.join(fixes)}", flush=True)
        except reddit.RedditError as exc:
            print(f"token refresh needed: {exc}", flush=True)
            tok = reddit.read_token()
        except Exception as exc:
            print(f"sighting {r['id']} failed: {exc}", flush=True)
        tried.add(r["id"])
        if n % 5 == 0:
            json.dump(sorted(tried), open(state, "w"))
            print(f"progress: {n}/{len(todo)} tried, {repaired} repaired", flush=True)
    json.dump(sorted(tried), open(state, "w"))
    print(f"done: repaired {repaired} of {len(todo)} media rows", flush=True)


def main() -> None:
    conn = db.connect(get_settings().db_path)
    tried = set()
    if os.path.exists(STATE):
        tried = set(json.load(open(STATE)))
    rows = conn.execute(
        """SELECT id, title, reddit_post_id, description, lat, created_at
           FROM sightings s
           WHERE source='reddit' AND reddit_post_id IS NOT NULL
             AND status IN ('live','deleted_by_user','removed_on_reddit')
             AND (
               (NOT EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id)
                AND NOT EXISTS (SELECT 1 FROM yt_jobs y WHERE y.sighting_id = s.id
                                AND y.status != 'failed'))
               OR (s.lat IS NULL AND length(TRIM(COALESCE(s.description,''))) < 30)
             )
           ORDER BY sighted_at DESC""").fetchall()
    todo = [r for r in rows if r["id"] not in tried]
    print(f"{len(rows)} media-less sightings, {len(todo)} to try", flush=True)
    tok = reddit.read_token()
    recovered = files = 0
    for n, r in enumerate(todo, 1):
        try:
            hit = find_archive_post(tok, r["title"], r["reddit_post_id"])
            if hit:
                post, orig_text = hit
                has_media = conn.execute(
                    "SELECT 1 FROM media WHERE sighting_id=? LIMIT 1",
                    (r["id"],)).fetchone()
                added = 0 if has_media else attach_media(conn, r["id"], post)
                repaired = repair_from_text(conn, r, orig_text)
                if added or repaired:
                    recovered += 1
                    files += added
                    print(f"RECOVERED sighting {r['id']} ({r['reddit_post_id']}) "
                          f"from archive {post['id']}: {added} file(s)"
                          f"{', text/geo repaired' if repaired else ''}", flush=True)
        except reddit.RedditError as exc:
            print(f"token refresh needed: {exc}", flush=True)
            tok = reddit.read_token()
        except Exception as exc:
            print(f"sighting {r['id']} failed: {exc}", flush=True)
        tried.add(r["id"])
        if n % 5 == 0:
            json.dump(sorted(tried), open(STATE, "w"))
            print(f"progress: {n}/{len(todo)} tried, {recovered} recovered", flush=True)
    json.dump(sorted(tried), open(STATE, "w"))
    print(f"done: recovered media for {recovered} sightings ({files} files) "
          f"of {len(todo)} tried", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "media":
        main_media_rows()
    else:
        main()
