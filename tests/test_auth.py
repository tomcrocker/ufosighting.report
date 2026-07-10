import httpx
import pytest
import respx

from app import auth, reddit_oauth


def test_login_url_contains_oauth_params():
    url = reddit_oauth.login_url("state-xyz")
    assert url.startswith("https://www.reddit.com/api/v1/authorize?")
    assert "client_id=webapp-id" in url
    assert "state=state-xyz" in url
    assert "duration=temporary" in url
    assert "scope=identity+submit" in url


@respx.mock
def test_exchange_code_returns_token():
    route = respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    assert reddit_oauth.exchange_code("code-1") == "tok-1"
    sent = route.calls[0].request
    assert b"grant_type=authorization_code" in sent.content
    assert sent.headers["User-Agent"].startswith("web:report.ufosighting")


@respx.mock
def test_exchange_code_error_raises():
    respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )
    with pytest.raises(reddit_oauth.AuthError):
        reddit_oauth.exchange_code("bad")


@respx.mock
def test_fetch_username():
    respx.get("https://oauth.reddit.com/api/v1/me").mock(
        return_value=httpx.Response(200, json={"name": "tmosh"})
    )
    assert reddit_oauth.fetch_username("tok-1") == "tmosh"


def test_session_roundtrip_and_expiry(db_conn):
    sid = auth.create_session(db_conn, "tester", "tok-abc", ttl_seconds=3600)
    sess = auth.get_session(db_conn, sid)
    assert sess.username == "tester" and sess.access_token == "tok-abc"

    expired = auth.create_session(db_conn, "old", "tok-old", ttl_seconds=-10)
    assert auth.get_session(db_conn, expired) is None
    # expired session row must be deleted
    n = db_conn.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (expired,)).fetchone()[0]
    assert n == 0


def test_get_session_unknown_id(db_conn):
    assert auth.get_session(db_conn, "nope") is None


def test_csrf_deterministic_and_session_bound():
    a = auth.csrf_for("sid-1")
    assert a == auth.csrf_for("sid-1")
    assert a != auth.csrf_for("sid-2")
    assert len(a) == 32


def test_draft_roundtrip(db_conn):
    auth.save_draft(db_conn, "tester", {"title": "hello", "media_json": "[]"})
    assert auth.load_draft(db_conn, "tester") == {"title": "hello", "media_json": "[]"}
    auth.save_draft(db_conn, "tester", {"title": "updated"})
    assert auth.load_draft(db_conn, "tester") == {"title": "updated"}
    auth.delete_draft(db_conn, "tester")
    assert auth.load_draft(db_conn, "tester") is None
