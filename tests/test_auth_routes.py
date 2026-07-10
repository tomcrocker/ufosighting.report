import httpx
import respx

from app import auth


def _mock_reddit_login(username="witness1"):
    respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-9"})
    )
    respx.get("https://oauth.reddit.com/api/v1/me").mock(
        return_value=httpx.Response(200, json={"name": username})
    )


def test_login_redirects_to_reddit_with_state_cookie(client):
    r = client.get("/auth/login?next=/submit", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://www.reddit.com/api/v1/authorize?")
    assert "oauth_state" in r.cookies


@respx.mock
def test_callback_creates_session_and_redirects(client):
    _mock_reddit_login()
    client.get("/auth/login?next=/submit", follow_redirects=False)
    state = client.cookies["oauth_state"].split("|")[0]
    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/submit"
    assert "sid" in r.cookies


def test_callback_rejects_state_mismatch(client):
    client.get("/auth/login", follow_redirects=False)
    r = client.get("/auth/callback?code=abc&state=WRONG", follow_redirects=False)
    assert r.status_code == 400


def test_callback_handles_user_denial(client):
    r = client.get("/auth/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 400


@respx.mock
def test_open_redirect_blocked(client):
    _mock_reddit_login()
    client.get("/auth/login?next=//evil.example", follow_redirects=False)
    state = client.cookies["oauth_state"].split("|")[0]
    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_logout_deletes_session(logged_in, app_db):
    sid = logged_in.sid
    assert auth.get_session(app_db, sid) is not None
    r = logged_in.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    assert auth.get_session(app_db, sid) is None
