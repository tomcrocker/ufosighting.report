#!/usr/bin/env python3
"""Export visible r/UFOs Sighting posts (last N days) from the ufosarchive DB
into a JSONL manifest for backfill_archive.py, uploading each post's media to
the ufosighting R2 bucket. Runs on the DEV VM (192.168.8.224) — archive DB +
downloaded media live here, and CDN/yt-dlp fetches come from a residential IP.

Zero Reddit OAuth API calls: text/comments from archive.db; media from the
archive's files (2026+) or Reddit's public CDN (older posts).

    nohup python3 export_sightings.py --days 365 \
        --out /tmp/sightings_export.jsonl > /tmp/export_sightings.log 2>&1 &

Resume-safe: post ids already in the output file are skipped on rerun.
R2 creds: ~/ufosighting-yt/config.json (same file the yt worker uses)."""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

import boto3

ARCHIVE_DB = "/opt/reddit-archive/data/archive.db"
MEDIA_BASE = "/opt/reddit-archive/media"
CONFIG_PATH = os.path.expanduser("~/ufosighting-yt/config.json")

MAX_BYTES = 200 * 1024 * 1024
MAX_ITEMS = 20
CDN_SLEEP = 0.5
UA = "Mozilla/5.0 (X11; Linux x86_64) ufosighting-archive-export/1.0"

MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp"}
EXT_CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4"}
YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\s()\[\]]*&)?v=|shorts/|live/|embed/|v/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})")


def find_youtube(*texts):
    for t in texts:
        m = YT_RE.search(t or "")
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    return None


def _best_rep_url(mpd_xml, mpd_url, want):
    """Highest-bandwidth video/audio BaseURL from a DASH MPD (the ufosighting
    ingest.py port — v.redd.it's manifest needs no auth, unlike reddit.com)."""
    import xml.etree.ElementTree as ET
    from urllib.parse import urljoin
    ns = {"m": "urn:mpeg:dash:schema:mpd:2011"}
    try:
        root = ET.fromstring(mpd_xml)
    except ET.ParseError:
        return None
    best = None
    for aset in root.findall(".//m:AdaptationSet", ns):
        ctype = (aset.get("contentType") or "").lower()
        mtype = (aset.get("mimeType") or "").lower()
        for rep in aset.findall("m:Representation", ns):
            rmime = (rep.get("mimeType") or mtype).lower()
            if not (want in ctype or want in rmime or want in mtype):
                continue
            bw = int(rep.get("bandwidth") or 0)
            base = rep.find("m:BaseURL", ns)
            if base is None or not base.text:
                continue
            if best is None or bw > best[0]:
                best = (bw, base.text.strip())
    return urljoin(mpd_url, best[1]) if best else None


def vreddit_download(url, td):
    """v.redd.it DASH download + ffmpeg mux; returns mp4 path or raises."""
    vid = url.split("v.redd.it/")[1].split("/")[0].split("?")[0]
    mpd_url = f"https://v.redd.it/{vid}/DASHPlaylist.mpd"
    req = urllib.request.Request(mpd_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        mpd = resp.read().decode("utf-8", "replace")
    vurl = _best_rep_url(mpd, mpd_url, "video")
    if not vurl:
        raise RuntimeError("no video representation in MPD")
    aurl = _best_rep_url(mpd, mpd_url, "audio")
    vpath, apath, out = (os.path.join(td, n) for n in ("v.tmp", "a.tmp", "v.mp4"))
    cdn_get(vurl, vpath)
    if aurl:
        try:
            cdn_get(aurl, apath)
        except Exception:
            aurl = None
    if aurl:
        proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", vpath,
                               "-i", apath, "-c", "copy", out],
                              capture_output=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("ffmpeg mux failed")
    else:
        os.rename(vpath, out)
    if os.path.getsize(out) > MAX_BYTES:
        raise RuntimeError("over size cap")
    return out


def cdn_get(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
        size = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise RuntimeError("over size cap")
            f.write(chunk)
    time.sleep(CDN_SLEEP)
    return os.path.getsize(path) > 0


class Exporter:
    def __init__(self, cfg):
        self.s3 = boto3.client("s3", endpoint_url=cfg["r2_endpoint"],
                               aws_access_key_id=cfg["r2_access_key"],
                               aws_secret_access_key=cfg["r2_secret_key"],
                               region_name="auto")
        self.bucket = cfg["r2_bucket"]

    def upload(self, path, key):
        ct = EXT_CT.get(os.path.splitext(key)[1].lower(), "application/octet-stream")
        with open(path, "rb") as f:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=f, ContentType=ct)

    def media_for_post(self, conn, post, td):
        """Returns (media list, yt_url, error). Order: archived files → CDN."""
        pid = post["id"]
        out = []
        rows = conn.execute(
            "SELECT * FROM media WHERE post_id=? AND downloaded=1 "
            "ORDER BY COALESCE(gallery_index, 0)", (pid,)).fetchall()
        if rows:
            try:
                for i, m in enumerate(rows[:MAX_ITEMS]):
                    # downloaded=1 with NULL local_path exists in the wild
                    if not m["local_path"]:
                        continue
                    src = os.path.join(MEDIA_BASE, m["local_path"])
                    if not os.path.exists(src) or os.path.getsize(src) > MAX_BYTES:
                        continue
                    ext = os.path.splitext(src)[1].lower() or ".jpg"
                    key = f"uploads/arc/{pid}_{i}{ext}"
                    self.upload(src, key)
                    kind = "video" if m["media_type"] == "video" else "image"
                    out.append({"key": key, "kind": kind})
            except Exception as exc:
                return out, None, str(exc)[:200]
            if out:
                return out, None, None

        url = post["url"] or ""
        yt = find_youtube(url, post["selftext"])
        if yt:
            return [], yt, None
        try:
            if "v.redd.it" in url:
                mp4 = vreddit_download(url, td)
                key = f"uploads/arc/{pid}_0.mp4"
                self.upload(mp4, key)
                return [{"key": key, "kind": "video"}], None, None
            if post["is_gallery"] and post["media_metadata"]:
                meta = json.loads(post["media_metadata"])
                for i, (mid, item) in enumerate(list(meta.items())[:MAX_ITEMS]):
                    if item.get("e") != "Image":
                        continue
                    ext = MIME_EXT.get(item.get("m"), ".jpg")
                    p = os.path.join(td, f"g{i}{ext}")
                    cdn_get(f"https://i.redd.it/{mid}{ext}", p)
                    key = f"uploads/arc/{pid}_{i}{ext}"
                    self.upload(p, key)
                    out.append({"key": key, "kind": "image"})
                return out, None, None
            low = url.lower().split("?")[0]
            if "i.redd.it" in url or low.endswith(tuple(EXT_CT)):
                ext = os.path.splitext(low)[1] or ".jpg"
                p = os.path.join(td, f"i{ext}")
                cdn_get(url, p)
                key = f"uploads/arc/{pid}_0{ext}"
                self.upload(p, key)
                return [{"key": key, "kind": "image"}], None, None
        except Exception as exc:
            return out, None, str(exc)[:200]
        return out, None, None


def comments_for(conn, pid, author):
    def rows(where, args):
        return conn.execute(
            f"""SELECT id, author, body, score, created_utc FROM comments
                WHERE post_id=? AND body IS NOT NULL AND TRIM(body) != ''
                  AND body NOT IN ('[deleted]','[removed]') {where}
                ORDER BY score DESC LIMIT 10""", (pid, *args)).fetchall()
    op = [r["body"] for r in rows("AND author = ?", (author,))] if author else []
    top = [{"id": r["id"], "author": r["author"], "body": r["body"],
            "score": r["score"] or 0, "created_utc": r["created_utc"] or 0,
            "permalink": f"/r/UFOs/comments/{pid}/_/{r['id']}/"}
           for r in rows("AND author NOT IN ('AutoModerator','[deleted]')", ())]
    return op, top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--from", dest="date_from", default="",
                    help="YYYY-MM-DD lower bound (overrides --days)")
    ap.add_argument("--to", dest="date_to", default="",
                    help="YYYY-MM-DD upper bound (default: now)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        exp = Exporter(json.load(f))
    done = set()
    if os.path.exists(args.out):
        with open(args.out) as f:
            done = {json.loads(l)["id"] for l in f if l.strip()}
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lo = (time.mktime(time.strptime(args.date_from, "%Y-%m-%d"))
          if args.date_from else time.time() - args.days * 86400)
    hi = (time.mktime(time.strptime(args.date_to, "%Y-%m-%d"))
          if args.date_to else time.time())
    posts = conn.execute(
        """SELECT id, title, author, selftext, created_utc, score, num_comments,
                  url, permalink, is_gallery, media_metadata
           FROM posts WHERE subreddit='UFOs' AND link_flair_text='Sighting'
             AND created_utc >= ? AND created_utc < ?
             AND COALESCE(removed,0)=0 AND COALESCE(deleted,0)=0
           ORDER BY created_utc""", (lo, hi)).fetchall()
    if args.limit:
        posts = posts[:args.limit]
    todo = [p for p in posts if p["id"] not in done]
    print(f"export: {len(posts)} posts, {len(todo)} to do "
          f"({len(done)} already exported)", flush=True)

    with open(args.out, "a") as outf:
        for n, post in enumerate(todo, 1):
            with tempfile.TemporaryDirectory(prefix="ufoexp_") as td:
                media, yt_url, err = exp.media_for_post(conn, post, td)
            op, top = comments_for(conn, post["id"], post["author"])
            outf.write(json.dumps({
                "id": post["id"], "title": post["title"], "author": post["author"],
                "selftext": post["selftext"], "created_utc": post["created_utc"],
                "score": post["score"], "num_comments": post["num_comments"],
                "url": post["url"], "op_comments": op, "top_comments": top,
                "media": media, "yt_url": yt_url, "media_error": err,
            }) + "\n")
            outf.flush()
            if n % 25 == 0:
                print(f"export: {n}/{len(todo)} done", flush=True)
    print(f"export finished: {len(todo)} rows appended to {args.out}", flush=True)


if __name__ == "__main__":
    main()
