"""Edge-caching headers: anonymous public surfaces advertise cacheability,
everything session-bound or mutating stays uncached, big JSON is gzipped."""
from tests.test_public import seed

CACHE_HEADER = "public, max-age=300, stale-while-revalidate=3600"


def test_public_pages_cacheable(client):
    for path in ("/", "/map", "/guide", "/investigate", "/sitemap.xml",
                 "/feed.xml", "/api/pins"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == CACHE_HEADER, path


def test_sighting_page_cacheable(client, app_db):
    sid = seed(app_db)
    resp = client.get(f"/sighting/{sid}/bright-orb-over-the-lake")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == CACHE_HEADER


def test_wizard_and_admin_not_cacheable(client):
    for path in ("/submit", "/admin/login"):
        resp = client.get(path)
        assert "max-age=300" not in (resp.headers.get("cache-control") or ""), path


def test_session_cookie_disables_caching(client):
    resp = client.get("/", cookies={"sid": "whatever"})
    assert resp.headers.get("cache-control") is None


def test_big_json_gzipped(client):
    resp = client.get("/api/pins", headers={"Accept-Encoding": "gzip"})
    # httpx transparently decompresses; the header proves the wire encoding
    assert resp.status_code == 200
    if len(resp.content) >= 1024:
        assert resp.headers.get("content-encoding") == "gzip"
