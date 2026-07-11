import httpx
import respx

from app import posting
from tests.test_db import _insert_sighting


def _seed_ready(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("UPDATE sightings SET status='pending_verify', reddit_username='witness1', "
                    "verify_token='tok123' WHERE id=?", (sid,))
    db_conn.commit()
    return sid


@respx.mock
def test_post_sighting_verified(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_zzz"}}}))
    sid = _seed_ready(db_conn)
    pid = posting.post_sighting(db_conn, sid, verified=True)
    assert pid == "zzz"
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["reddit_post_id"] == "zzz"
    assert row["username_verified"] == 1 and row["verify_token"] is None


@respx.mock
def test_post_sighting_self_reported_attribution(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    route = respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_qq"}}}))
    sid = _seed_ready(db_conn)
    posting.post_sighting(db_conn, sid, verified=False)
    body = route.calls[0].request.content
    assert b"self-reported" in body
