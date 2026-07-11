"""Native media posting: upload-lease API + image/gallery/video submits.

kind=image/video submits are ASYNC — Reddit returns only a websocket URL and
creates the post after processing. Instead of a websocket client we poll the
bot's /submitted listing for the new title (wait_for_post_id); the same
lookup doubles as retry dedupe in posting.post_sighting."""
import re
import time
from dataclasses import dataclass

import httpx

from app.reddit import RateLimited, RedditError, TokenExpired, _headers

ASSET_URL = "https://oauth.reddit.com/api/media/asset.json"
SUBMIT_URL = "https://oauth.reddit.com/api/submit"
GALLERY_URL = "https://oauth.reddit.com/api/submit_gallery_post.json"
COMMENT_URL = "https://oauth.reddit.com/api/comment"

IMAGE_POLL_TIMEOUT = 30
VIDEO_POLL_TIMEOUT = 120  # transcode can be slow


@dataclass
class Asset:
    asset_id: str
    url: str


def _check_errors(payload: dict) -> None:
    errors = (payload.get("json", {}) or {}).get("errors") or []
    if errors:
        code = errors[0][0]
        msg = errors[0][1] if len(errors[0]) > 1 else code
        if code == "RATELIMIT":
            raise RateLimited(msg)
        raise RedditError(f"{code}: {msg}")


def upload_asset(token: str, filename: str, mimetype: str, data: bytes) -> Asset:
    resp = httpx.post(ASSET_URL, data={"filepath": filename, "mimetype": mimetype},
                      headers=_headers(token), timeout=30)
    if resp.status_code != 200:
        raise RedditError(f"asset lease failed: HTTP {resp.status_code}")
    j = resp.json()
    args = j.get("args") or {}
    action = args.get("action") or ""
    if action.startswith("//"):
        action = "https:" + action
    fields = {f["name"]: f["value"] for f in args.get("fields", [])}
    asset_id = (j.get("asset") or {}).get("asset_id") or ""
    if not action or not asset_id:
        raise RedditError("unexpected asset lease response")
    up = httpx.post(action, data=fields, files={"file": (filename, data, mimetype)},
                    timeout=300)
    if up.status_code not in (200, 201, 204):
        raise RedditError(f"asset upload failed: HTTP {up.status_code}")
    return Asset(asset_id=asset_id, url=f"{action}/{fields.get('key', '')}")


def _submit_async(token: str, data: dict) -> None:
    resp = httpx.post(SUBMIT_URL, data=data, headers=_headers(token), timeout=30)
    if resp.status_code == 401:
        raise TokenExpired("Reddit session expired")
    if resp.status_code != 200:
        raise RedditError(f"reddit submit failed: HTTP {resp.status_code}")
    _check_errors(resp.json())


def _base_submit(subreddit: str, title: str, kind: str, flair_id: str) -> dict:
    data = {"sr": subreddit, "kind": kind, "title": title[:300], "api_type": "json",
            "sendreplies": "true", "resubmit": "true"}
    if flair_id:
        data["flair_id"] = flair_id
    return data


def submit_image(token: str, *, subreddit: str, title: str, image_url: str,
                 flair_id: str = "") -> None:
    data = _base_submit(subreddit, title, "image", flair_id)
    data["url"] = image_url
    _submit_async(token, data)


def submit_video(token: str, *, subreddit: str, title: str, video_url: str,
                 poster_url: str, flair_id: str = "") -> None:
    data = _base_submit(subreddit, title, "video", flair_id)
    data["url"] = video_url
    data["video_poster_url"] = poster_url
    _submit_async(token, data)


def submit_gallery(token: str, *, subreddit: str, title: str, asset_ids: list[str],
                   flair_id: str = "") -> str:
    payload = {"sr": subreddit, "title": title[:300], "api_type": "json",
               "sendreplies": True, "show_error_list": True,
               "nsfw": False, "spoiler": False,
               "items": [{"media_id": a, "caption": "", "outbound_url": ""}
                         for a in asset_ids]}
    if flair_id:
        payload["flair_id"] = flair_id
    resp = httpx.post(GALLERY_URL, json=payload, headers=_headers(token), timeout=30)
    if resp.status_code == 401:
        raise TokenExpired("Reddit session expired")
    if resp.status_code != 200:
        raise RedditError(f"gallery submit failed: HTTP {resp.status_code}")
    body = resp.json()
    _check_errors(body)
    url = ((body.get("json", {}) or {}).get("data") or {}).get("url") or ""
    m = re.search(r"/comments/([a-z0-9]+)", url)
    if not m:
        raise RedditError(f"unexpected gallery response: {url!r}")
    return m.group(1)


def find_recent_post_id(token: str, *, username: str, title: str,
                        max_age_s: int = 300) -> str | None:
    resp = httpx.get(f"https://oauth.reddit.com/user/{username}/submitted",
                     params={"limit": 10, "sort": "new"},
                     headers=_headers(token), timeout=20)
    if resp.status_code != 200:
        return None
    now = time.time()
    for child in resp.json().get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("title") == title and now - float(d.get("created_utc", 0)) <= max_age_s:
            return d.get("id")
    return None


def wait_for_post_id(token: str, *, username: str, title: str, timeout_s: int,
                     interval_s: int = 3, sleep=time.sleep, clock=time.time) -> str | None:
    deadline = clock() + timeout_s
    while clock() < deadline:
        pid = find_recent_post_id(token, username=username, title=title, max_age_s=600)
        if pid:
            return pid
        sleep(interval_s)
    return None


def comment(token: str, *, post_id: str, text: str) -> None:
    resp = httpx.post(COMMENT_URL,
                      data={"api_type": "json", "thing_id": f"t3_{post_id}", "text": text},
                      headers=_headers(token), timeout=30)
    if resp.status_code != 200:
        raise RedditError(f"comment failed: HTTP {resp.status_code}")
    _check_errors(resp.json())
