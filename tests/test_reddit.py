import httpx
import pytest
import respx

from app import reddit


@pytest.fixture(autouse=True)
def _reset_script_token():
    reddit._token_cache.clear()
    yield


def _submit_ok(post_fullname="t3_1abcde"):
    return httpx.Response(
        200,
        json={"json": {"errors": [], "data": {"name": post_fullname, "url": "https://reddit.com/x"}}},
    )


@respx.mock
def test_submit_post_success_returns_bare_id():
    route = respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    post_id = reddit.submit_post(
        "tok-1", subreddit="UFOs_sandbox", title="Orb over lake", body="body text", flair_id="flair-123"
    )
    assert post_id == "1abcde"
    sent = route.calls[0].request
    assert b"sr=UFOs_sandbox" in sent.content
    assert b"kind=self" in sent.content
    assert b"flair_id=flair-123" in sent.content
    assert sent.headers["Authorization"] == "bearer tok-1"


@respx.mock
def test_submit_post_omits_empty_flair():
    route = respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T" * 10, body="b")
    assert b"flair_id" not in route.calls[0].request.content


@respx.mock
def test_submit_post_ratelimit_raises():
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "you are doing that too much. try again in 9 minutes.", "ratelimit"]
        ]}})
    )
    with pytest.raises(reddit.RateLimited, match="9 minutes"):
        reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T" * 10, body="b")


@respx.mock
def test_submit_post_401_raises_token_expired():
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=httpx.Response(401))
    with pytest.raises(reddit.TokenExpired):
        reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T" * 10, body="b")


@respx.mock
def test_submit_post_403_is_plain_error_not_token_expired():
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=httpx.Response(403))
    with pytest.raises(reddit.RedditError) as exc_info:
        reddit.submit_post("tok-1", subreddit="UFOs_sandbox", title="T" * 10, body="b")
    assert not isinstance(exc_info.value, reddit.TokenExpired)


@respx.mock
def test_script_token_cached_across_calls():
    route = respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "stok", "expires_in": 3600})
    )
    assert reddit.script_token() == "stok"
    assert reddit.script_token() == "stok"
    assert route.call_count == 1


@respx.mock
def test_fetch_posts_info_parses_children():
    respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "stok", "expires_in": 3600})
    )
    respx.get("https://oauth.reddit.com/api/info").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "aaa", "removed_by_category": None, "score": 42, "num_comments": 7}},
            {"data": {"id": "bbb", "removed_by_category": "moderator", "score": 1, "num_comments": 0}},
        ]}})
    )
    infos = reddit.fetch_posts_info(["aaa", "bbb"])
    assert infos["aaa"].score == 42 and infos["aaa"].removed_by_category is None
    assert infos["bbb"].removed_by_category == "moderator"


def test_fetch_posts_info_empty_list_no_network():
    assert reddit.fetch_posts_info([]) == {}


def test_status_mapping():
    f = reddit.status_from_removed_by_category
    assert f(None) == "live"
    assert f("deleted") == "deleted_by_user"
    for rbc in ("moderator", "automod_filtered", "reddit", "spam", "content_takedown"):
        assert f(rbc) == "removed_on_reddit"


@respx.mock
def test_send_message_ok():
    route = respx.post("https://oauth.reddit.com/api/compose").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}})
    )
    reddit.send_message("tok", to="witness1", subject="Verify", text="link")
    body = route.calls[0].request.content
    assert b"to=witness1" in body and b"api_type=json" in body


@respx.mock
def test_send_message_ratelimit():
    respx.post("https://oauth.reddit.com/api/compose").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "try again in 3 minutes", "ratelimit"]]}})
    )
    with pytest.raises(reddit.RateLimited):
        reddit.send_message("tok", to="x", subject="s", text="t")


@respx.mock
def test_list_flair_posts_parses():
    respx.get("https://oauth.reddit.com/r/UFOs_sandbox/search").mock(
        return_value=httpx.Response(200, json={"data": {"after": "t3_next", "children": [
            {"data": {"id": "aaa", "title": "Orb", "author": "u1", "link_flair_text": "Sighting"}}]}})
    )
    posts, after = reddit.list_flair_posts("tok", subreddit="UFOs_sandbox", flair="Sighting")
    assert posts[0]["id"] == "aaa" and after == "t3_next"


@respx.mock
def test_read_token_falls_back_to_bot_when_unset():
    # READ_USERNAME is empty in the test env → read_token == script_token
    route = respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "bot-tok", "expires_in": 3600}))
    assert reddit.read_token() == "bot-tok"
    assert b"username=modbot" in route.calls[0].request.content


@respx.mock
def test_read_token_uses_read_account_with_separate_cache(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("READ_USERNAME", "reader")
    monkeypatch.setenv("READ_PASSWORD", "read-pw")
    get_settings.cache_clear()
    tokens = {"modbot": "bot-tok", "reader": "read-tok"}

    def responder(request):
        body = request.content.decode()
        user = [u for u in tokens if f"username={u}" in body][0]
        return httpx.Response(200, json={"access_token": tokens[user], "expires_in": 3600})

    respx.post("https://www.reddit.com/api/v1/access_token").mock(side_effect=responder)
    assert reddit.read_token() == "read-tok"
    assert reddit.script_token() == "bot-tok"   # separate cache entries
    assert reddit.read_token() == "read-tok"    # cached, no extra grant


@respx.mock
def test_approve_posts_thing_id():
    route = respx.post("https://oauth.reddit.com/api/approve").mock(
        return_value=httpx.Response(200, json={}))
    reddit.approve("tok", post_id="1abc")
    assert b"id=t3_1abc" in route.calls[0].request.content
