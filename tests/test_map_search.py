from tests.test_public import seed


def test_pins_returns_live_coords_only(client, app_db):
    sid = seed(app_db, title="Pinned", lat=48.8, lon=-124.1)
    seed(app_db, title="No coords", lat=None, lon=None)
    seed(app_db, title="Hidden pin", lat=10.0, lon=10.0, status="hidden_by_admin")
    pins = client.get("/api/pins").json()["pins"]
    assert pins == [[sid, 48.8, -124.1, "2026-07-01"]]


def test_pin_detail_popup_payload(client, app_db):
    sid = seed(app_db, title="Pinned", lat=48.8, lon=-124.1)
    hidden = seed(app_db, title="Hidden pin", lat=10.0, lon=10.0,
                  status="hidden_by_admin")
    detail = client.get(f"/api/pins/{sid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["title"] == "Pinned"
    assert body["url"] == f"/sighting/{sid}/pinned"
    assert body["date"] == "2026-07-01"
    assert client.get(f"/api/pins/{hidden}").status_code == 404


def test_map_page_renders(client):
    r = client.get("/map")
    assert r.status_code == 200
    assert 'id="map"' in r.text


def test_search_finds_by_text(client, app_db):
    seed(app_db, title="Black triangle over Phoenix", description="Three lights in formation " * 10)
    seed(app_db, title="Green flash at sea", description="A brilliant green flash " * 10)
    r = client.get("/search?q=triangle phoenix")
    assert "Black triangle over Phoenix" in r.text
    assert "Green flash at sea" not in r.text


def test_search_excludes_non_live(client, app_db):
    seed(app_db, title="Secret triangle", status="hidden_by_admin")
    r = client.get("/search?q=triangle")
    assert "Secret triangle" not in r.text


def test_search_handles_quotes_safely(client, app_db):
    seed(app_db)
    r = client.get('/search?q="orb AND (weird')
    assert r.status_code == 200


def test_sitemap_lists_live_sightings(client, app_db):
    sid = seed(app_db)
    seed(app_db, title="Hidden entry", status="hidden_by_admin")
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert f"/sighting/{sid}/" in r.text
    assert r.text.count("<url>") == 5  # home, map, investigate, guide + 1 sighting


def test_robots(client):
    r = client.get("/robots.txt")
    assert "Sitemap:" in r.text
