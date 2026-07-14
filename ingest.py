"""Ingest Sighting-flaired posts from the subreddit into the gallery, extracting
date/time/location via LLM + geocoding. Run by ufosighting-ingest.timer;
`--backfill` walks the last 30 days once."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import httpx

from app import db, extract, geocode, helpers, r2, reddit, search, ytdetect
from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
CT_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
BACKFILL_PAGE_SLEEP_SECONDS = 3
PER_POST_SLEEP_SECONDS = 2
BACKFILL_DAYS = 30
VIDEO_MAX_BYTES = 200 * 1024 * 1024  # protect the 1GB-RAM VM


def _fetch_image(url: str):
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"User-Agent": get_settings().user_agent})
    if resp.status_code != 200:
        return None
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    return resp.content, ct, CT_EXT.get(ct, ".jpg")


def _download_to_file(url: str, path: str) -> bool:
    """Stream a URL to disk (1MB chunks); True if non-empty file written."""
    try:
        with httpx.stream("GET", url, timeout=60, follow_redirects=True,
                          headers={"User-Agent": get_settings().user_agent}) as resp:
            if resp.status_code != 200:
                return False
            size = 0
            with open(path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    size += len(chunk)
                    if size > VIDEO_MAX_BYTES:
                        return False
                    f.write(chunk)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except httpx.HTTPError:
        return False


def _mux_or_copy(video_path: str, audio_path: str | None, out_path: str) -> bool:
    """Mux video+audio via ffmpeg stream-copy; plain move when no audio."""
    if not audio_path:
        shutil.move(video_path, out_path)
        return True
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-i", audio_path,
         "-c", "copy", out_path],
        capture_output=True, timeout=300,
    )
    return proc.returncode == 0 and os.path.exists(out_path)


def _best_rep_url_from_mpd(mpd_xml: str, mpd_url: str, want: str) -> str | None:
    """Pick the highest-bandwidth `video` or `audio` BaseURL from a DASH MPD.
    (Ported from ufosarchive's media_downloader — battle-tested on v.redd.it.)"""
    import xml.etree.ElementTree as ET
    from urllib.parse import urljoin
    try:
        ns = {"m": "urn:mpeg:dash:schema:mpd:2011"}
        root = ET.fromstring(mpd_xml)
        best = None
        for aset in root.findall(".//m:AdaptationSet", ns):
            content_type = (aset.get("contentType") or "").lower()
            mime_type = (aset.get("mimeType") or "").lower()
            for rep in aset.findall("m:Representation", ns):
                rep_mime = (rep.get("mimeType") or mime_type).lower()
                if not (want in content_type or want in rep_mime or want in mime_type):
                    continue
                bw = int(rep.get("bandwidth") or 0)
                baseurl_el = rep.find("m:BaseURL", ns)
                if baseurl_el is None or not baseurl_el.text:
                    continue
                if best is None or bw > best[0]:
                    best = (bw, baseurl_el.text.strip())
        return urljoin(mpd_url, best[1]) if best else None
    except ET.ParseError:
        return None


def _download_vreddit(rv: dict) -> tuple[bytes, str, str] | None:
    """Download a v.redd.it video: DASH manifest (video+audio muxed) preferred,
    fallback_url (video-only) otherwise."""
    dash_url = rv.get("dash_url")
    fallback_url = (rv.get("fallback_url") or "").split("?")[0]
    with tempfile.TemporaryDirectory() as td:
        vpath, apath, opath = (os.path.join(td, n) for n in ("v.mp4", "a.mp4", "out.mp4"))
        audio = None
        video_url = None
        if dash_url:
            try:
                resp = httpx.get(dash_url, timeout=30,
                                 headers={"User-Agent": get_settings().user_agent})
                if resp.status_code == 200:
                    video_url = _best_rep_url_from_mpd(resp.text, dash_url, "video")
                    audio_url = _best_rep_url_from_mpd(resp.text, dash_url, "audio")
                    if audio_url and _download_to_file(audio_url, apath):
                        audio = apath
            except httpx.HTTPError:
                pass
        if not _download_to_file(video_url or fallback_url, vpath):
            return None
        if not _mux_or_copy(vpath, audio, opath):
            return None
        if os.path.getsize(opath) > VIDEO_MAX_BYTES:
            return None
        with open(opath, "rb") as f:
            return f.read(), "video/mp4", ".mp4"


def download_media(post: dict) -> list[tuple[bytes, str, str]]:
    # v.redd.it video first — most r/UFOs Sighting posts are videos
    rv = ((post.get("secure_media") or {}).get("reddit_video")
          or (post.get("media") or {}).get("reddit_video")
          or (post.get("preview") or {}).get("reddit_video_preview"))
    if rv and (rv.get("fallback_url") or rv.get("dash_url")):
        vid = _download_vreddit(rv)
        return [vid] if vid else []

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


def ingest_post(conn, post: dict, token=None, op_comments: list[str] | None = None) -> bool:
    pid = post["id"]
    if conn.execute("SELECT 1 FROM sightings WHERE reddit_post_id=?", (pid,)).fetchone():
        return False
    post_created_iso = datetime.fromtimestamp(post.get("created_utc", 0), timezone.utc).strftime(ISO)

    # archive-fed backfills supply op_comments directly — no API fetch then
    if op_comments is None:
        op_comments = fetch_op_comments(token, post)
    text = extract.combine_post_text(post, op_comments)
    clamped = extract.validate_and_clamp(extract.extract_fields(text),
                                         post_created_iso=post_created_iso)

    coords = None
    for q in geocode.candidates(clamped.get("location_text") or "",
                                clamped.get("city"), clamped.get("country")):
        coords = geocode.forward(conn, q)
        if coords:
            break

    sighted_at, tz_name = build_sighted_at(clamped, post_created_iso)
    title = (post.get("title") or "Untitled sighting")[:300]
    description = (post.get("selftext") or "").strip() or (clamped.get("summary") or "")
    city = (coords or {}).get("city") or clamped.get("city")
    country = (coords or {}).get("country") or clamped.get("country")

    # Media BEFORE the row, then one commit for row+media: an interruption
    # mid-download leaves no row behind, so the next run retries the post
    # instead of dedup permanently skipping a media-less entry.
    try:
        media_items = download_media(post)
    except Exception as exc:
        print(f"ingest media for {pid} failed: {exc}")
        media_items = []

    # Only date/time/location are trustworthy from free-text posts — richer
    # structured fields (shape, object count, duration) are reserved for the
    # site's own submission wizard where the witness states them directly.
    cur = conn.execute(
        """INSERT INTO sightings
             (source, reddit_username, title, description, sighted_at, tz_name,
              location_text, city, country,
              lat, lon, reddit_post_id, reddit_score, reddit_num_comments, status)
           VALUES ('reddit',?,?,?,?,?,?,?,?,?,?,?,?,?, 'live')""",
        (post.get("author") or "unknown", title, description, sighted_at, tz_name,
         clamped.get("location_text") or "", city, country,
         (coords or {}).get("lat"), (coords or {}).get("lon"), pid,
         int(post.get("score") or 0), int(post.get("num_comments") or 0)),
    )
    sid = cur.lastrowid
    try:
        for i, (data, ct, ext) in enumerate(media_items):
            now = datetime.now(timezone.utc)
            key = f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"
            r2.put_bytes(key, data, ct)
            kind = "video" if ct.startswith("video/") else "image"
            conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) "
                         "VALUES (?,?,?,?)", (sid, key, kind, i))
    except Exception as exc:
        print(f"ingest media upload for {pid} failed: {exc}")
    # No reddit-hosted media? A YouTube link (post URL or body) becomes a
    # download job for the local-VM worker — YouTube blocks this VM's IP.
    if not media_items:
        yt_url = ytdetect.find_youtube_url(post)
        if yt_url:
            conn.execute("INSERT OR IGNORE INTO yt_jobs (sighting_id, url) VALUES (?,?)",
                         (sid, yt_url))
    conn.commit()
    search.index_sightings(conn, [sid])
    return True


def _ping_indexnow(conn, pids: list[str]) -> None:
    """Notify IndexNow of newly-ingested sighting URLs. Only the daily ingest
    calls this (not backfills) — a few posts per run, never a flood."""
    if not pids:
        return
    from app import helpers, indexnow
    s = get_settings()
    rows = conn.execute(
        f"SELECT id, title FROM sightings WHERE reddit_post_id IN "
        f"({','.join('?' * len(pids))})", pids).fetchall()
    urls = [f"{s.base_url}/sighting/{r['id']}/{helpers.slugify(r['title'])}"
            for r in rows]
    try:
        indexnow.submit_urls(urls)
    except Exception as exc:
        print(f"ingest: indexnow submit failed: {exc}")


def ingest_once(conn, *, limit=100, after=None) -> dict:
    s = get_settings()
    token = reddit.read_token()
    posts, _after = reddit.list_flair_posts(token, subreddit=s.ingest_subreddit,
                                            flair="Sighting", limit=limit, after=after)
    added_pids = []
    for p in posts:
        if ingest_post(conn, p, token=token):
            added_pids.append(p["id"])
            time.sleep(PER_POST_SLEEP_SECONDS)
    _ping_indexnow(conn, added_pids)
    return {"seen": len(posts), "added": len(added_pids)}


def main(backfill: bool = False) -> None:
    conn = db.connect(get_settings().db_path)
    try:
        if backfill:
            cutoff = time.time() - BACKFILL_DAYS * 86400
            after, total, stop = None, 0, False
            while not stop:
                s = get_settings()
                token = reddit.read_token()
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
