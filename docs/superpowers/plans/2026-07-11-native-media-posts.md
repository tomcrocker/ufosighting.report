# Native Media in Bot Posts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot posts site submissions to Reddit as native image/gallery/video posts (video-first, either/or per post), with the details/attribution body as a first comment; any pre-submit failure falls back to today's self post.

**Architecture:** New `app/reddit_media.py` wraps the upload-lease API (`/api/media/asset.json` → S3 POST), the three submit shapes, an id-poll for async image/video submits, and `/api/comment`. `app/posting.py` picks the plan (video → image → gallery → self) and orchestrates fallback + retry dedupe. Spec: `docs/superpowers/specs/2026-07-11-native-media-posts-design.md`.

**Tech Stack:** Python 3.12, httpx, respx (tests), boto3 (R2 reads), pytest.

## Global Constraints

- Video-first; either photos OR video per post, never both. Gallery cap 20.
- Attribution comment uses existing `format_post_body` with `media_urls=[]`.
- Poll timeouts: image 30s, video 120s, interval 3s.
- Accepted-submit poll timeout raises `RedditError` (NO fallback — retry dedupes by title, window 300s).
- `RateLimited` always propagates (self post would be rate-limited too).
- `pytest -q` green before each commit.

---

### Task 1: `app/reddit_media.py`

**Files:**
- Create: `app/reddit_media.py`
- Test: `tests/test_reddit_media.py`

**Interfaces (produced):**
- `Asset(asset_id: str, url: str)` dataclass
- `upload_asset(token, filename, mimetype, data: bytes) -> Asset`
- `submit_image(token, *, subreddit, title, image_url, flair_id="") -> None`
- `submit_video(token, *, subreddit, title, video_url, poster_url, flair_id="") -> None`
- `submit_gallery(token, *, subreddit, title, asset_ids, flair_id="") -> str` (post id)
- `find_recent_post_id(token, *, username, title, max_age_s=300) -> str | None`
- `wait_for_post_id(token, *, username, title, timeout_s, interval_s=3, sleep=time.sleep, clock=time.time) -> str | None`
- `comment(token, *, post_id, text) -> None`
- Constants `IMAGE_POLL_TIMEOUT = 30`, `VIDEO_POLL_TIMEOUT = 120`

- [ ] **Step 1: Failing tests** (`tests/test_reddit_media.py`, respx style as test_reddit.py)

```python
import httpx
import pytest
import respx

from app import reddit, reddit_media

LEASE = {
    "args": {"action": "//reddit-uploaded-media.s3-accelerate.amazonaws.com",
             "fields": [{"name": "key", "value": "rte_images/abc"},
                        {"name": "policy", "value": "xyz"}]},
    "asset": {"asset_id": "asset123", "websocket_url": "wss://x"},
}


@respx.mock
def test_upload_asset_happy_path():
    respx.post("https://oauth.reddit.com/api/media/asset.json").mock(
        return_value=httpx.Response(200, json=LEASE))
    s3 = respx.post("https://reddit-uploaded-media.s3-accelerate.amazonaws.com").mock(
        return_value=httpx.Response(201))
    a = reddit_media.upload_asset("tok", "img.jpg", "image/jpeg", b"bytes")
    assert a.asset_id == "asset123"
    assert a.url == "https://reddit-uploaded-media.s3-accelerate.amazonaws.com/rte_images/abc"
    assert s3.called


@respx.mock
def test_upload_asset_lease_failure_raises():
    respx.post("https://oauth.reddit.com/api/media/asset.json").mock(
        return_value=httpx.Response(500))
    with pytest.raises(reddit.RedditError):
        reddit_media.upload_asset("tok", "img.jpg", "image/jpeg", b"bytes")


@respx.mock
def test_upload_asset_s3_failure_raises():
    respx.post("https://oauth.reddit.com/api/media/asset.json").mock(
        return_value=httpx.Response(200, json=LEASE))
    respx.post("https://reddit-uploaded-media.s3-accelerate.amazonaws.com").mock(
        return_value=httpx.Response(403))
    with pytest.raises(reddit.RedditError):
        reddit_media.upload_asset("tok", "img.jpg", "image/jpeg", b"bytes")


@respx.mock
def test_submit_image_sends_kind_and_url():
    route = respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}}))
    reddit_media.submit_image("tok", subreddit="UFOs_sandbox", title="T",
                              image_url="https://u/img", flair_id="f1")
    body = route.calls[0].request.content
    assert b"kind=image" in body and b"flair_id=f1" in body


@respx.mock
def test_submit_video_includes_poster():
    route = respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}}))
    reddit_media.submit_video("tok", subreddit="s", title="T",
                              video_url="https://u/v.mp4", poster_url="https://u/p.jpg")
    body = route.calls[0].request.content
    assert b"kind=video" in body and b"video_poster_url" in body


@respx.mock
def test_submit_ratelimit_raises():
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "slow down", "ratelimit"]]}}))
    with pytest.raises(reddit.RateLimited):
        reddit_media.submit_image("tok", subreddit="s", title="T", image_url="u")


@respx.mock
def test_submit_gallery_returns_post_id():
    respx.post("https://oauth.reddit.com/api/submit_gallery_post.json").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {
            "url": "https://www.reddit.com/r/s/comments/1gal01/t/", "id": "abc"}}}))
    pid = reddit_media.submit_gallery("tok", subreddit="s", title="T",
                                      asset_ids=["a1", "a2"])
    assert pid == "1gal01"


def _submitted(children):
    return httpx.Response(200, json={"data": {"children": [
        {"data": d} for d in children]}})


@respx.mock
def test_find_recent_post_id_matches_title_and_age(monkeypatch):
    import time as _t
    now = _t.time()
    respx.get("https://oauth.reddit.com/user/bot/submitted").mock(
        return_value=_submitted([
            {"title": "Other", "id": "x1", "created_utc": now},
            {"title": "Match", "id": "x2", "created_utc": now - 10},
            {"title": "Match", "id": "x3", "created_utc": now - 9999},
        ]))
    assert reddit_media.find_recent_post_id("tok", username="bot", title="Match") == "x2"
    assert reddit_media.find_recent_post_id("tok", username="bot", title="Nope") is None


@respx.mock
def test_wait_for_post_id_polls_until_found():
    import time as _t
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return _submitted([])
        return _submitted([{"title": "T", "id": "found1", "created_utc": _t.time()}])

    respx.get("https://oauth.reddit.com/user/bot/submitted").mock(side_effect=responder)
    pid = reddit_media.wait_for_post_id("tok", username="bot", title="T",
                                        timeout_s=60, sleep=lambda s: None)
    assert pid == "found1" and calls["n"] == 3


@respx.mock
def test_wait_for_post_id_timeout_returns_none():
    respx.get("https://oauth.reddit.com/user/bot/submitted").mock(
        return_value=_submitted([]))
    t = {"now": 0.0}

    def clock():
        t["now"] += 10
        return t["now"]

    assert reddit_media.wait_for_post_id("tok", username="bot", title="T",
                                         timeout_s=30, sleep=lambda s: None,
                                         clock=clock) is None


@respx.mock
def test_comment_posts_thing_id():
    route = respx.post("https://oauth.reddit.com/api/comment").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}}))
    reddit_media.comment("tok", post_id="1abc", text="hello")
    assert b"thing_id=t3_1abc" in route.calls[0].request.content
```

- [ ] **Step 2: Run → import error.** `pytest tests/test_reddit_media.py -q`
- [ ] **Step 3: Implement `app/reddit_media.py`**

```python
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
```

- [ ] **Step 4: `pytest tests/test_reddit_media.py -q` → pass; full suite green.**
- [ ] **Step 5: Commit** — `git commit -m "feat: reddit media upload-lease API client"`

### Task 2: posting.py rework

**Files:**
- Modify: `app/posting.py`
- Test: `tests/test_posting.py` (additions; existing tests updated for the new dedupe call)

**Interfaces:**
- Consumes: everything Task 1 produces; `r2.client().get_object`.
- Produces: `post_sighting` signature unchanged. New module-privates `_mime(key)`, `_fetch_r2(key)`, `_post_native(row_id, media, title, flair_id) -> str | None`.

- [ ] **Step 1: Failing tests** (append; stub `reddit.script_token`, `reddit_media.*`, `_fetch_r2`)

```python
def _mk_media(conn, sid, *rows):
    for i, (key, kind, thumb) in enumerate(rows):
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind, thumb_key, sort_order)"
                     " VALUES (?,?,?,?,?)", (sid, key, kind, thumb, i))
    conn.commit()


def _native_stubs(monkeypatch, calls):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting, "_fetch_r2", lambda key: b"bytes-" + key.encode())
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id",
                        lambda *a, **k: None)
    monkeypatch.setattr(posting.reddit_media, "upload_asset",
                        lambda tok, fn, mt, data: calls.setdefault("uploads", []).append((fn, mt))
                        or posting.reddit_media.Asset(f"as{len(calls['uploads'])}", f"https://u/{fn}"))
    monkeypatch.setattr(posting.reddit_media, "comment",
                        lambda tok, *, post_id, text: calls.update(comment=(post_id, text)))


def test_video_first_native_post(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)          # reuse existing helper in this file
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None),
              ("uploads/b.mp4", "video", "thumbs/b.jpg"))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_video",
                        lambda tok, **k: calls.update(video=k))
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id",
                        lambda tok, **k: "vid001")
    pid = posting.post_sighting(db_conn, sid, verified=True)
    assert pid == "vid001"
    assert "video_url" in calls["video"] and "poster_url" in calls["video"]
    assert calls["comment"][0] == "vid001"
    assert "ufosighting.report" in calls["comment"][1]


def test_single_image_native(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image",
                        lambda tok, **k: calls.update(image=k))
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id",
                        lambda tok, **k: "img001")
    assert posting.post_sighting(db_conn, sid, verified=False) == "img001"


def test_multi_image_gallery(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None),
              ("uploads/b.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_gallery",
                        lambda tok, **k: calls.update(gallery=k) or "gal001")
    assert posting.post_sighting(db_conn, sid, verified=True) == "gal001"
    assert calls["gallery"]["asset_ids"] == ["as1", "as2"]


def test_upload_failure_falls_back_to_self_post(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id", lambda *a, **k: None)
    monkeypatch.setattr(posting, "_fetch_r2", lambda key: b"x")

    def boom(*a, **k):
        raise posting.reddit.RedditError("lease failed")

    monkeypatch.setattr(posting.reddit_media, "upload_asset", boom)
    monkeypatch.setattr(posting.reddit, "submit_post",
                        lambda tok, **k: "self001")
    assert posting.post_sighting(db_conn, sid, verified=True) == "self001"


def test_poll_timeout_raises_no_fallback(db_conn, monkeypatch):
    import pytest
    sid = _mk_sighting(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: None)
    fell_back = []
    monkeypatch.setattr(posting.reddit, "submit_post",
                        lambda tok, **k: fell_back.append(1) or "self001")
    with pytest.raises(posting.reddit.RedditError):
        posting.post_sighting(db_conn, sid, verified=True)
    assert not fell_back
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] != "live"


def test_retry_adopts_existing_post(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id",
                        lambda *a, **k: "adopted1")
    monkeypatch.setattr(posting.reddit_media, "comment",
                        lambda tok, *, post_id, text: calls.update(comment=post_id))
    submitted = []
    monkeypatch.setattr(posting.reddit_media, "upload_asset",
                        lambda *a, **k: submitted.append(1))
    assert posting.post_sighting(db_conn, sid, verified=True) == "adopted1"
    assert not submitted


def test_comment_failure_nonfatal(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img9")

    def bad_comment(tok, *, post_id, text):
        raise posting.reddit.RedditError("comment blocked")

    monkeypatch.setattr(posting.reddit_media, "comment", bad_comment)
    assert posting.post_sighting(db_conn, sid, verified=True) == "img9"


def test_no_media_keeps_self_post(db_conn, monkeypatch):
    sid = _mk_sighting(db_conn)
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id", lambda *a, **k: None)
    monkeypatch.setattr(posting.reddit, "submit_post", lambda tok, **k: "self777")
    assert posting.post_sighting(db_conn, sid, verified=True) == "self777"
```

Also update any existing `test_posting.py` tests that stub only `reddit.submit_post`: add the two stubs `reddit.script_token → "tok"` and `reddit_media.find_recent_post_id → None`.

- [ ] **Step 2: Run → failures.**
- [ ] **Step 3: Rework `app/posting.py`**

```python
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


def _post_native(token: str, media: list, title: str) -> str | None:
    """Try a native media post. Returns the post id, None to request the
    self-post fallback, and raises RedditError only after Reddit accepted an
    async submit but the post never showed up in the poll window (no fallback
    then — it may still be transcoding; retry adopts it by title)."""
    s = get_settings()
    videos = [m for m in media if m["kind"] == "video" and m["thumb_key"]]
    images = [m for m in media if m["kind"] == "image"]
    try:
        if videos:
            v = videos[0]
            video = reddit_media.upload_asset(
                token, v["r2_key"].rsplit("/", 1)[-1], _mime(v["r2_key"]),
                _fetch_r2(v["r2_key"]))
            poster = reddit_media.upload_asset(
                token, "poster.jpg", "image/jpeg", _fetch_r2(v["thumb_key"]))
            reddit_media.submit_video(token, subreddit=s.subreddit, title=title,
                                      video_url=video.url, poster_url=poster.url,
                                      flair_id=s.sighting_flair_id)
            timeout = reddit_media.VIDEO_POLL_TIMEOUT
        elif len(images) == 1:
            img = images[0]
            asset = reddit_media.upload_asset(
                token, img["r2_key"].rsplit("/", 1)[-1], _mime(img["r2_key"]),
                _fetch_r2(img["r2_key"]))
            reddit_media.submit_image(token, subreddit=s.subreddit, title=title,
                                      image_url=asset.url,
                                      flair_id=s.sighting_flair_id)
            timeout = reddit_media.IMAGE_POLL_TIMEOUT
        elif images:
            assets = [reddit_media.upload_asset(
                token, m["r2_key"].rsplit("/", 1)[-1], _mime(m["r2_key"]),
                _fetch_r2(m["r2_key"])) for m in images[:20]]
            return reddit_media.submit_gallery(
                token, subreddit=s.subreddit, title=title,
                asset_ids=[a.asset_id for a in assets],
                flair_id=s.sighting_flair_id)
        else:
            return None
    except reddit.RateLimited:
        raise
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
    pre-submit. Shared by the verify fast-lane and mod approval. Raises
    reddit.RateLimited / reddit.RedditError without changing status so the
    caller can retry.
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

    # A previous attempt may have posted but timed out on the id poll —
    # adopt that post instead of double-posting.
    post_id = reddit_media.find_recent_post_id(
        token, username=s.script_username, title=title)
    if post_id is None and media:
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
```

- [ ] **Step 4: `pytest -q` green (fix existing posting tests per note above).**
- [ ] **Step 5: Commit** — `git commit -m "feat: bot posts native image/gallery/video with details comment"`

### Task 3: Deploy + live verification (ops)

- [ ] **Step 1:** Get user approval, then `bash deploy/deploy.sh`.
- [ ] **Step 2:** Live test against r/tmoshtest: submit via the wizard (or insert a pending row) with (a) one photo, (b) a video; verify native rendering, flair, first comment with attribution, DB `reddit_post_id`, gallery card unaffected.
- [ ] **Step 3:** Push; update memory.
