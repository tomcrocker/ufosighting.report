import httpx
import respx

from app import auth
from tests.test_public import seed


def _admin(client, app_db):
    sid = auth.create_session(app_db, "tmosh", "tok", 3600)
    client.cookies.set("sid", sid)
    return sid


def test_review_lists_pending(client, app_db):
    seed(app_db, title="Queued one", status="pending_review", reddit_username="w")
    _admin(client, app_db)
    r = client.get("/admin/review")
    assert r.status_code == 200 and "Queued one" in r.text


def test_review_requires_admin(client):
    assert client.get("/admin/review").status_code == 404


@respx.mock
def test_approve_posts_and_lives(client, app_db, monkeypatch):
    monkeypatch.setattr("app.posting.reddit.script_token", lambda: "bot")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_qq"}}}))
    sid = seed(app_db, status="pending_review", reddit_username="w")
    tok = _admin(client, app_db)
    r = client.post(f"/admin/review/{sid}/approve",
                    data={"csrf_token": auth.csrf_for(tok)}, follow_redirects=False)
    assert r.status_code == 303
    row = app_db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["reddit_post_id"] == "qq"


def test_reject(client, app_db):
    sid = seed(app_db, status="pending_review", reddit_username="w")
    tok = _admin(client, app_db)
    client.post(f"/admin/review/{sid}/reject", data={"csrf_token": auth.csrf_for(tok)},
                follow_redirects=False)
    assert app_db.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()[0] == "rejected"


def test_review_action_bad_csrf(client, app_db):
    sid = seed(app_db, status="pending_review", reddit_username="w")
    _admin(client, app_db)
    r = client.post(f"/admin/review/{sid}/reject", data={"csrf_token": "forged"})
    assert r.status_code == 403
