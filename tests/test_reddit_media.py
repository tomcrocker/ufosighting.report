import time as _t

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
def test_find_recent_post_id_matches_title_and_age():
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
