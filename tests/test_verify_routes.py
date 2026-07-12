import httpx
import respx

from tests.test_public import seed


def _pending(app_db, token="tok-abc"):
    sid = seed(app_db, status="pending_verify", reddit_username="witness1")
    app_db.execute("UPDATE sightings SET verify_token=? WHERE id=?", (token, sid))
    app_db.commit()
    return sid


@respx.mock
def test_valid_token_posts_and_goes_live(client, app_db, monkeypatch):
    monkeypatch.setattr("app.posting.reddit.script_token", lambda: "bot")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_pp"}}}))
    respx.get("https://oauth.reddit.com/api/info").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"removed_by_category": None}}]}}))
    sid = _pending(app_db)
    r = client.get("/verify/tok-abc")
    assert r.status_code == 200 and "live" in r.text.lower()
    row = app_db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["username_verified"] == 1 and row["reddit_post_id"] == "pp"


def test_unknown_token_friendly(client):
    r = client.get("/verify/nope")
    assert r.status_code == 200 and "no longer valid" in r.text.lower()


def test_used_token_friendly(client, app_db):
    # a live sighting with no token — the link was already consumed
    seed(app_db, status="live", reddit_username="w")
    r = client.get("/verify/anything")
    assert r.status_code == 200 and "no longer valid" in r.text.lower()
