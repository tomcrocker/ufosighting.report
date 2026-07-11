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
