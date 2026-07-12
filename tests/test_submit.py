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
        "rule_out": "Checked FlightRadar24 and Stellarium — nothing matches; silent and too fast.",
        "confirm_eyewitness": "1", "confirm_no_fixed_cam": "1",
        "confirm_not_screen": "1", "confirm_in_focus": "1",
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


# --- r/UFOs guideline gates (rule-out statement + confirmations) ---

RULE_OUT = "Checked FlightRadar24 — no aircraft nearby; too fast and silent for a drone."


def gform(csrf, **over):
    d = form(csrf)
    d.update({"rule_out": RULE_OUT, "confirm_eyewitness": "1",
              "confirm_no_fixed_cam": "1", "confirm_not_screen": "1",
              "confirm_in_focus": "1"})
    d.update(over)
    return d


def test_missing_rule_out_rejected(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, rule_out=""), cookies={"csrf": csrf})
    assert r.status_code == 422 and "rule out" in r.text.lower()


def test_short_rule_out_rejected(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, rule_out="not a plane"), cookies={"csrf": csrf})
    assert r.status_code == 422


def test_missing_eyewitness_confirm_rejected(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, confirm_eyewitness=""), cookies={"csrf": csrf})
    assert r.status_code == 422 and "own eyes" in r.text.lower()


def test_media_confirms_required_only_with_media(client, app_db):
    csrf = get_csrf(client)
    # media attached + missing focus confirmation -> rejected
    r = client.post("/submit", data=gform(csrf, confirm_in_focus=""), cookies={"csrf": csrf})
    assert r.status_code == 422
    # no media -> the three camera confirmations are not required
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, media_json="[]", confirm_no_fixed_cam="",
                                          confirm_not_screen="", confirm_in_focus=""),
                    cookies={"csrf": csrf})
    assert r.status_code == 200


def test_rule_out_stored_and_in_post_body(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf), cookies={"csrf": csrf})
    assert r.status_code == 200
    row = app_db.execute("SELECT rule_out FROM sightings ORDER BY id DESC LIMIT 1").fetchone()
    assert row["rule_out"] == RULE_OUT
    from app import helpers
    body = helpers.format_post_body(
        dict(row) | {"tz_name": "UTC", "description": "d", "movement": [],
                     "sensors": [], "witness_background": []},
        sighted_local="x", location_line="y", media_urls=[], gallery_url="u",
        attribution="")
    assert "Why not a common object" in body and RULE_OUT in body


def test_country_only_location_rejected(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, location_text="France", city="",
                                          country="France", lat="", lon=""),
                    cookies={"csrf": csrf})
    assert r.status_code == 422 and "precise" in r.text.lower()
    # case-insensitive
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, location_text="  UNITED STATES ",
                                          city="", country="", lat="", lon=""),
                    cookies={"csrf": csrf})
    assert r.status_code == 422


def test_city_named_like_region_still_accepted(client, app_db):
    csrf = get_csrf(client)
    # "Singapore" is a city-state — must remain submittable
    r = client.post("/submit", data=gform(csrf, location_text="Singapore",
                                          city="Singapore", country="Singapore"),
                    cookies={"csrf": csrf})
    assert r.status_code == 200


def test_coordinates_as_location_fill_latlon(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, location_text="48.4284, -123.3656",
                                          city="", country="", lat="", lon=""),
                    cookies={"csrf": csrf})
    assert r.status_code == 200
    row = app_db.execute("SELECT lat, lon, location_text FROM sightings "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    assert abs(row["lat"] - 48.4284) < 1e-4 and abs(row["lon"] + 123.3656) < 1e-4


def test_bogus_coordinates_rejected(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, location_text="123.9, -200.0",
                                          city="", country="", lat="", lon=""),
                    cookies={"csrf": csrf})
    assert r.status_code == 422


def test_short_title_rejected(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, title="Orbs!"), cookies={"csrf": csrf})
    assert r.status_code == 422 and "15" in r.text


def test_capture_device_stored_and_in_post_body(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, capture_device="iPhone 16 Pro"),
                    cookies={"csrf": csrf})
    assert r.status_code == 200
    row = app_db.execute("SELECT capture_device FROM sightings ORDER BY id DESC LIMIT 1").fetchone()
    assert row["capture_device"] == "iPhone 16 Pro"
    from app import helpers
    body = helpers.format_post_body(
        {"capture_device": "iPhone 16 Pro", "tz_name": "UTC", "description": "d",
         "movement": [], "sensors": [], "witness_background": []},
        sighted_local="x", location_line="y", media_urls=[], gallery_url="u",
        attribution="")
    assert "Captured on:** iPhone 16 Pro" in body


def test_capture_device_optional_and_capped(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf, capture_device="x" * 300),
                    cookies={"csrf": csrf})
    assert r.status_code == 200
    row = app_db.execute("SELECT capture_device FROM sightings ORDER BY id DESC LIMIT 1").fetchone()
    assert len(row["capture_device"]) == 100


def test_submitted_page_names_bot_and_gates(client, app_db):
    csrf = get_csrf(client)
    r = client.post("/submit", data=gform(csrf), cookies={"csrf": csrf})
    assert r.status_code == 200
    assert "u/modbot" in r.text                 # SCRIPT_USERNAME in the test env
    assert "not live yet" in r.text
    assert "6 hours" in r.text                  # verify window default
