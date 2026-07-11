import json

import httpx
import pytest
import respx

MEDIA_KEY = "uploads/2026/07/" + "a" * 32 + ".jpg"
STORY = ("A silent amber orb hovered above the treeline for two minutes, pulsing softly, "
         "then shot straight up and vanished. No sound, clear sky, three of us watched it.")


def form(csrf):
    return {
        "csrf_token": csrf, "cf-turnstile-response": "x",
        "reddit_username": "witness1",
        "title": "Amber orb over the lake", "description": STORY,
        "sighted_date": "2026-07-01", "sighted_time": "22:15", "tz_name": "America/Vancouver",
        "location_text": "Lake Cowichan, BC", "city": "Lake Cowichan", "country": "Canada",
        "lat": "48.82", "lon": "-124.05", "location_obscured": "",
        "duration_value": "120", "duration_unit": "seconds", "witnesses": "2",
        "shape": "sphere", "num_objects": "1", "distance": "above the trees",
        "apparent_size": "dime", "movement_json": json.dumps(["hovering"]),
        "sensors_json": "[]", "background_json": "[]",
        "has_wings": "", "has_rotors": "", "has_plume": "", "makes_noise": "",
        "media_json": json.dumps([{"key": MEDIA_KEY, "kind": "image", "width": 100,
                                   "height": 80, "size_bytes": 1234}]),
    }


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr("app.routes.submit.r2.head_exists", lambda k: True)
    monkeypatch.setattr("app.routes.submit.turnstile.verify", lambda t, ip=None: True)
    from app.routes import submit as sm
    sm._geocode_cache.clear()


def get_csrf(client):
    r = client.get("/submit")
    assert r.status_code == 200
    return client.cookies["csrf"]


def test_submit_anonymous_reaches_wizard(client):
    r = client.get("/submit")
    assert r.status_code == 200
    assert 'name="reddit_username"' in r.text
    assert "csrf" in client.cookies


@respx.mock
def test_happy_path_creates_pending_verify_and_dms(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.submit.reddit.script_token", lambda: "bot-tok")
    dm = respx.post("https://oauth.reddit.com/api/compose").mock(
        return_value=httpx.Response(200, json={"json": {"errors": []}}))
    csrf = get_csrf(client)
    r = client.post("/submit", data=form(csrf), follow_redirects=False)
    assert r.status_code == 200 and "inbox" in r.text.lower()
    row = app_db.execute("SELECT * FROM sightings WHERE id=1").fetchone()
    assert row["status"] == "pending_verify"
    assert row["reddit_username"] == "witness1"
    assert row["verify_token"] and row["reddit_post_id"] is None
    sent = dm.calls[0].request.content
    assert b"to=witness1" in sent and b"verify" in sent.lower()


def test_bad_username_rejected(client, app_db):
    csrf = get_csrf(client)
    f = form(csrf); f["reddit_username"] = "no"
    r = client.post("/submit", data=f)
    assert r.status_code == 422
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_bad_csrf_rejected(client):
    get_csrf(client)
    f = form("forged")
    assert client.post("/submit", data=f).status_code == 403


def test_turnstile_failure_rejected(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.submit.turnstile.verify", lambda t, ip=None: False)
    csrf = get_csrf(client)
    r = client.post("/submit", data=form(csrf))
    assert r.status_code == 400
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


@respx.mock
def test_rate_limit_trips(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.submit.reddit.script_token", lambda: "t")
    monkeypatch.setattr("app.routes.submit.reddit.send_message", lambda *a, **k: None)
    csrf = get_csrf(client)
    for _ in range(5):
        client.post("/submit", data=form(csrf))
    r = client.post("/submit", data=form(csrf))
    assert r.status_code == 429


@respx.mock
def test_dm_failure_is_non_fatal(client, app_db, monkeypatch):
    # DM raises, but the sighting is still saved as pending_verify
    monkeypatch.setattr("app.routes.submit.reddit.script_token", lambda: "t")
    def boom(*a, **k):
        from app import reddit
        raise reddit.RedditError("spam filtered")
    monkeypatch.setattr("app.routes.submit.reddit.send_message", boom)
    csrf = get_csrf(client)
    r = client.post("/submit", data=form(csrf))
    assert r.status_code == 200
    assert app_db.execute("SELECT status FROM sightings WHERE id=1").fetchone()[0] == "pending_verify"


def test_geocode_no_login(client):
    assert client.get("/api/geocode?q=ab").json() == {"results": []}


def test_presign_no_login(client):
    r = client.post("/api/presign", json={"filename": "a.jpg", "content_type": "image/jpeg",
                                          "size_bytes": 1000})
    assert r.status_code == 200
