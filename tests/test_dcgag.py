from app.config import get_settings
from tests.test_public import seed

DC = {"cf-ipcountry": "US", "cf-region-code": "DC"}


def _enable(monkeypatch):
    monkeypatch.setenv("DC_GAG_ENABLED", "1")
    get_settings.cache_clear()


def test_dc_visitor_gets_the_gag(client, app_db, monkeypatch):
    seed(app_db, title="A real sighting")
    _enable(monkeypatch)
    r = client.get("/", headers=DC)
    assert r.status_code == 200
    assert "Washington" in r.text and "declassify" in r.text.lower()
    assert "A real sighting" not in r.text          # real site suppressed


def test_non_dc_visitor_unaffected(client, app_db, monkeypatch):
    seed(app_db, title="Visible sighting")
    _enable(monkeypatch)
    r = client.get("/", headers={"cf-ipcountry": "US", "cf-region-code": "CA"})
    assert "Visible sighting" in r.text and "declassify" not in r.text.lower()


def test_disabled_by_default(client, app_db):
    seed(app_db, title="Default visible")
    r = client.get("/", headers=DC)                 # flag off -> inert
    assert "Default visible" in r.text


def test_bypass_cookie_lets_dc_through(client, app_db, monkeypatch):
    seed(app_db, title="Bypassed sighting")
    _enable(monkeypatch)
    client.cookies.set("dc_bypass", "1")
    r = client.get("/", headers=DC)
    assert "Bypassed sighting" in r.text


def test_reveal_sets_cookie_and_redirects(client, monkeypatch):
    _enable(monkeypatch)
    r = client.get("/dc/reveal", headers=DC, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "dc_bypass=" in r.headers.get("set-cookie", "")


def test_api_is_exempt(client, monkeypatch):
    _enable(monkeypatch)
    r = client.get("/api/pins", headers=DC)
    assert "declassify" not in r.text.lower()       # data endpoints never gagged


def test_post_is_not_gagged(client, monkeypatch):
    _enable(monkeypatch)
    r = client.post("/does-not-exist", headers=DC)  # gag is GET-only
    assert "declassify" not in r.text.lower()


def test_preview_always_renders_even_when_disabled(client):
    # flag off (default), no DC headers — /dc/preview still shows the gag
    r = client.get("/dc/preview")
    assert r.status_code == 200
    assert "Washington" in r.text and "declassify" in r.text.lower()
