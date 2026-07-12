import json

import httpx
import respx

from app import search
from app.config import get_settings
from tests.test_public import seed

MEILI = "http://127.0.0.1:7700"


def _enable(monkeypatch):
    monkeypatch.setenv("MEILI_URL", MEILI)
    monkeypatch.setenv("MEILI_KEY", "masterkey")
    get_settings.cache_clear()


def test_disabled_is_noop(client, app_db):
    # test env has no MEILI_URL -> every hook is a silent no-op
    assert search.enabled() is False
    search.index_sightings(app_db, [1, 2])       # must not raise / no network
    search.delete_sightings([1])
    search.apply_settings()


@respx.mock
def test_index_upserts_public_rows(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db, title="Meili doc", shape="sphere", reddit_score=7)
    up = respx.post(f"{MEILI}/indexes/sightings/documents").mock(
        return_value=httpx.Response(202, json={"taskUid": 1}))
    search.index_sightings(app_db, [sid])
    assert up.called
    docs = json.loads(up.calls[0].request.content)
    d = docs[0]
    assert d["id"] == sid and d["shape"] == "sphere" and d["has_geo"] is True
    assert isinstance(d["sighted_ts"], int) and d["reddit_score"] == 7


@respx.mock
def test_index_deletes_nonpublic_rows(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db, title="Hidden", status="hidden_by_admin")
    dele = respx.post(f"{MEILI}/indexes/sightings/documents/delete-batch").mock(
        return_value=httpx.Response(202, json={"taskUid": 2}))
    search.index_sightings(app_db, [sid])
    assert dele.called
    assert json.loads(dele.calls[0].request.content) == [sid]


@respx.mock
def test_meili_failure_never_raises(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db)
    respx.post(f"{MEILI}/indexes/sightings/documents").mock(
        side_effect=httpx.ConnectError("down"))
    search.index_sightings(app_db, [sid])  # must swallow


@respx.mock
def test_apply_settings_payload(monkeypatch):
    _enable(monkeypatch)
    respx.put(f"{MEILI}/indexes").mock(return_value=httpx.Response(202, json={}))
    patch = respx.patch(f"{MEILI}/indexes/sightings/settings").mock(
        return_value=httpx.Response(202, json={}))
    search.apply_settings()
    body = json.loads(patch.calls[0].request.content)
    assert "sighted_ts" in body["sortableAttributes"]
    assert "has_geo" in body["filterableAttributes"]
    assert "uap" in body["synonyms"]["ufo"]


@respx.mock
def test_search_ids_builds_filters_and_sort(monkeypatch):
    _enable(monkeypatch)
    route = respx.post(f"{MEILI}/indexes/sightings/search").mock(
        return_value=httpx.Response(200, json={"hits": [{"id": 3}, {"id": 1}],
                                               "estimatedTotalHits": 2}))
    out = search.search_ids(shape="sphere", country="Canada", sort="top",
                            top_window="week", date_from="2026-07-01")
    assert out["ids"] == [3, 1] and out["total"] == 2
    body = json.loads(route.calls[0].request.content)
    assert "shape = 'sphere'" in body["filter"]
    assert "country = 'Canada'" in body["filter"]
    assert any(f.startswith("sighted_ts >= ") for f in body["filter"])
    assert body["sort"][0] == "reddit_score:desc"


@respx.mock
def test_search_ids_none_on_failure(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{MEILI}/indexes/sightings/search").mock(
        side_effect=httpx.ConnectError("down"))
    assert search.search_ids(q="orb") is None


def test_search_ids_none_when_disabled():
    assert search.search_ids(q="orb") is None


@respx.mock
def test_gallery_uses_meili_order(client, app_db, monkeypatch):
    _enable(monkeypatch)
    a = seed(app_db, title="First seeded entry", sighted_at="2026-07-01T05:00:00Z")
    b = seed(app_db, title="Second seeded entry", sighted_at="2026-07-05T05:00:00Z")
    # meili returns OLDER first — hydration must preserve that order
    respx.post(f"{MEILI}/indexes/sightings/search").mock(
        return_value=httpx.Response(200, json={"hits": [{"id": a}, {"id": b}],
                                               "estimatedTotalHits": 2}))
    text = client.get("/").text
    assert text.index("First seeded entry") < text.index("Second seeded entry")


@respx.mock
def test_pins_pass_has_geo(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db, lat=10.0, lon=20.0)
    route = respx.post(f"{MEILI}/indexes/sightings/search").mock(
        return_value=httpx.Response(200, json={"hits": [{"id": sid}],
                                               "estimatedTotalHits": 1}))
    pins = client.get("/api/pins").json()["pins"]
    assert len(pins) == 1
    body = json.loads(route.calls[0].request.content)
    assert "has_geo = true" in body["filter"]


@respx.mock
def test_search_redirects_to_gallery(client):
    r = client.get("/search?q=orb&shape=sphere", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/?q=orb&shape=sphere"


@respx.mock
def test_gallery_search_via_meili(client, app_db, monkeypatch):
    _enable(monkeypatch)
    sid = seed(app_db, title="Faceted orb result")
    route = respx.post(f"{MEILI}/indexes/sightings/search").mock(
        return_value=httpx.Response(200, json={
            "hits": [{"id": sid}], "estimatedTotalHits": 1}))
    r = client.get("/?q=orb")
    assert r.status_code == 200
    assert "Faceted orb result" in r.text
    assert "1</strong> result" in r.text
    # text queries use Meili relevance — no sort key in the request
    import json as _json
    body = _json.loads(route.calls[0].request.content)
    assert body["q"] == "orb" and "sort" not in body


def test_search_page_fallback_fts(client, app_db):
    # meili disabled -> FTS5 path still works
    seed(app_db, title="Fallback triangle result",
         description="A triangle over the bay " * 10)
    r = client.get("/?q=triangle")
    assert "Fallback triangle result" in r.text


@respx.mock
def test_admin_hide_deletes_from_index(client, app_db, monkeypatch):
    _enable(monkeypatch)
    from app import auth
    sid = seed(app_db)
    admin_sid = auth.create_session(app_db, "tmosh", "tok", 3600)
    client.cookies.set("sid", admin_sid)
    dele = respx.post(f"{MEILI}/indexes/sightings/documents/delete-batch").mock(
        return_value=httpx.Response(202, json={"taskUid": 9}))
    client.post(f"/admin/sighting/{sid}/action",
                data={"csrf_token": auth.csrf_for(admin_sid), "action": "hide"},
                follow_redirects=False)
    assert dele.called


@respx.mock
def test_reindex_indexes_public_rows(client, app_db, monkeypatch):
    _enable(monkeypatch)
    import reindex
    seed(app_db, title="Reindex me")
    seed(app_db, title="Skip me", status="hidden_by_admin")
    respx.put(f"{MEILI}/indexes").mock(return_value=httpx.Response(202, json={}))
    respx.patch(f"{MEILI}/indexes/sightings/settings").mock(
        return_value=httpx.Response(202, json={}))
    up = respx.post(f"{MEILI}/indexes/sightings/documents").mock(
        return_value=httpx.Response(202, json={"taskUid": 1}))
    monkeypatch.setattr(reindex.db, "connect", lambda p: app_db)
    reindex.main()
    docs = json.loads(up.calls[0].request.content)
    assert len(docs) == 1 and docs[0]["title"] == "Reindex me"
