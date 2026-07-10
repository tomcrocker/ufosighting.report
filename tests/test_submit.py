import json

import httpx
import pytest
import respx

from app import auth

MEDIA_KEY = "uploads/2026/07/" + "a" * 32 + ".jpg"

STORY = (
    "A silent orange orb hovered above the treeline for roughly two minutes, "
    "pulsing softly, then accelerated straight up and vanished in under a second. "
    "There was no sound at any point, the sky was clear, and no aircraft were visible."
)


def good_form(sid: str) -> dict:
    return {
        "csrf_token": auth.csrf_for(sid),
        "title": "Bright orb over the lake",
        "description": STORY,
        "sighted_date": "2026-07-01",
        "sighted_time": "22:15",
        "tz_name": "America/Vancouver",
        "location_text": "Lake Cowichan, BC",
        "city": "Lake Cowichan",
        "country": "Canada",
        "lat": "48.82512",
        "lon": "-124.05467",
        "location_obscured": "",
        "duration_value": "120",
        "duration_unit": "seconds",
        "witnesses": "2",
        "shape": "sphere",
        "num_objects": "2",
        "distance": "above the trees",
        "apparent_size": "golf ball",
        "movement_json": json.dumps(["hovering", "extremely fast"]),
        "sensors_json": json.dumps(["infrared"]),
        "background_json": json.dumps(["pilot"]),
        "has_wings": "no",
        "has_rotors": "no",
        "has_plume": "unsure",
        "makes_noise": "yes",
        "media_json": json.dumps(
            [{"key": MEDIA_KEY, "kind": "image", "width": 1920, "height": 1080, "size_bytes": 123456}]
        ),
    }


@pytest.fixture(autouse=True)
def _media_exists(monkeypatch):
    monkeypatch.setattr("app.routes.submit.r2.head_exists", lambda key: True)


@pytest.fixture(autouse=True)
def _clear_geocode_cache():
    from app.routes import submit as submit_routes
    submit_routes._geocode_cache.clear()


def _submit_ok():
    return httpx.Response(
        200, json={"json": {"errors": [], "data": {"name": "t3_1abcde", "url": "https://reddit/x"}}}
    )


def test_get_submit_anonymous_shows_login(client):
    r = client.get("/submit")
    assert r.status_code == 200
    assert "Continue with Reddit" in r.text


def test_get_submit_logged_in_shows_wizard(logged_in):
    r = logged_in.get("/submit")
    assert r.status_code == 200
    assert 'name="csrf_token"' in r.text
    assert 'name="media_json"' in r.text
    assert 'data-step="7"' in r.text
    assert "saucer" in r.text  # shape chips rendered


@respx.mock
def test_happy_path_posts_to_reddit_and_goes_live(logged_in, app_db):
    route = respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    r = logged_in.post("/submit", data=good_form(logged_in.sid), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sighting/1/bright-orb-over-the-lake"

    row = app_db.execute("SELECT * FROM sightings WHERE id=1").fetchone()
    assert row["status"] == "live"
    assert row["reddit_post_id"] == "1abcde"
    assert row["reddit_username"] == "tester"
    assert row["sighted_at"] == "2026-07-02T05:15:00Z"
    assert row["duration_seconds"] == 120
    assert row["num_objects"] == "2"
    assert row["apparent_size"] == "golf ball"
    assert json.loads(row["movement"]) == ["hovering", "extremely fast"]
    assert json.loads(row["sensors"]) == ["infrared"]
    assert json.loads(row["witness_background"]) == ["pilot"]
    assert row["has_plume"] == "unsure"
    assert row["location_obscured"] == 0
    media = app_db.execute("SELECT * FROM media WHERE sighting_id=1").fetchall()
    assert len(media) == 1 and media[0]["r2_key"] == MEDIA_KEY

    sent = route.calls[0].request
    assert b"sr=UFOs_sandbox" in sent.content
    assert sent.headers["Authorization"] == "bearer tok-abc"


def test_bad_csrf_rejected(logged_in):
    form = good_form(logged_in.sid)
    form["csrf_token"] = "forged"
    assert logged_in.post("/submit", data=form).status_code == 403


def test_story_too_short_rejected(logged_in, app_db):
    form = good_form(logged_in.sid)
    form["description"] = "Saw a light. It moved fast."
    r = logged_in.post("/submit", data=form)
    assert r.status_code == 422
    assert "150" in r.text
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_title_too_short_rerenders(logged_in, app_db):
    form = good_form(logged_in.sid)
    form["title"] = "hi"
    r = logged_in.post("/submit", data=form)
    assert r.status_code == 422
    assert "Title must be" in r.text
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def test_bad_media_key_rejected(logged_in):
    form = good_form(logged_in.sid)
    form["media_json"] = json.dumps([{"key": "../../etc/passwd", "kind": "image"}])
    assert logged_in.post("/submit", data=form).status_code == 422


@respx.mock
def test_obscured_location_rounds_coords_and_text(logged_in, app_db):
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    form = good_form(logged_in.sid)
    form["location_obscured"] = "1"
    r = logged_in.post("/submit", data=form, follow_redirects=False)
    assert r.status_code == 303
    row = app_db.execute("SELECT * FROM sightings WHERE id=1").fetchone()
    assert row["location_obscured"] == 1
    assert row["lat"] == 48.8 and row["lon"] == -124.1
    assert row["location_text"] == "Lake Cowichan, Canada"


@respx.mock
def test_invalid_chip_values_dropped(logged_in, app_db):
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=_submit_ok())
    form = good_form(logged_in.sid)
    form["shape"] = "mothership"
    form["movement_json"] = json.dumps(["hovering", "teleporting"])
    r = logged_in.post("/submit", data=form, follow_redirects=False)
    assert r.status_code == 303
    row = app_db.execute("SELECT * FROM sightings WHERE id=1").fetchone()
    assert row["shape"] is None
    assert json.loads(row["movement"]) == ["hovering"]


@respx.mock
def test_ratelimit_rolls_back_and_shows_message(logged_in, app_db):
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [
            ["RATELIMIT", "try again in 9 minutes", "ratelimit"]]}})
    )
    r = logged_in.post("/submit", data=good_form(logged_in.sid))
    assert r.status_code == 429
    assert "9 minutes" in r.text
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


@respx.mock
def test_token_expired_saves_draft_and_redirects_to_login(logged_in, app_db):
    respx.post("https://oauth.reddit.com/api/submit").mock(return_value=httpx.Response(401))
    r = logged_in.post("/submit", data=good_form(logged_in.sid), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login?next=/submit"
    draft = auth.load_draft(app_db, "tester")
    assert draft["title"] == "Bright orb over the lake"
    assert app_db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


@respx.mock
def test_geocode_proxies_nominatim_and_caches(logged_in):
    route = respx.get("https://nominatim.openstreetmap.org/search").mock(
        return_value=httpx.Response(200, json=[{
            "display_name": "Victoria, British Columbia, Canada",
            "lat": "48.4284", "lon": "-123.3656",
            "address": {"city": "Victoria", "country": "Canada"},
        }])
    )
    r = logged_in.get("/api/geocode?q=Victoria")
    assert r.status_code == 200
    result = r.json()["results"][0]
    assert result["city"] == "Victoria"
    assert result["country"] == "Canada"
    assert abs(result["lat"] - 48.4284) < 1e-6
    logged_in.get("/api/geocode?q=Victoria")
    assert route.call_count == 1  # second call served from cache


def test_geocode_requires_login(client):
    assert client.get("/api/geocode?q=Victoria").status_code == 401


def test_geocode_short_query_returns_empty(logged_in):
    assert logged_in.get("/api/geocode?q=ab").json() == {"results": []}
