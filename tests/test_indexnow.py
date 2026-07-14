import json

import httpx
import respx

from app import indexnow
from app.config import get_settings


def _set_key(monkeypatch, key="abc123def456"):
    monkeypatch.setenv("INDEXNOW_KEY", key)
    monkeypatch.setenv("BASE_URL", "https://ufosighting.report")
    get_settings.cache_clear()
    return key


def test_submit_disabled_without_key(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "")
    get_settings.cache_clear()
    out = indexnow.submit_urls(["https://ufosighting.report/a"])
    assert out["submitted"] == 0 and out["skipped"] is True


@respx.mock
def test_submit_posts_expected_payload(monkeypatch):
    key = _set_key(monkeypatch)
    route = respx.post(indexnow.ENDPOINT).mock(return_value=httpx.Response(200))
    out = indexnow.submit_urls(["https://ufosighting.report/sighting/1/a",
                                "https://ufosighting.report/sighting/2/b"])
    assert out["submitted"] == 2 and out["batches"] == 1
    body = json.loads(route.calls[0].request.content)
    assert body["key"] == key
    assert body["host"] == "ufosighting.report"
    assert body["keyLocation"] == f"https://ufosighting.report/{key}.txt"
    assert len(body["urlList"]) == 2


@respx.mock
def test_submit_batches_over_10k(monkeypatch):
    _set_key(monkeypatch)
    respx.post(indexnow.ENDPOINT).mock(return_value=httpx.Response(200))
    urls = [f"https://ufosighting.report/sighting/{i}/x" for i in range(23000)]
    out = indexnow.submit_urls(urls)
    assert out["batches"] == 3 and out["submitted"] == 23000


@respx.mock
def test_submit_dedupes(monkeypatch):
    _set_key(monkeypatch)
    respx.post(indexnow.ENDPOINT).mock(return_value=httpx.Response(200))
    out = indexnow.submit_urls(["https://x/a", "https://x/a", "https://x/b"])
    assert out["submitted"] == 2


@respx.mock
def test_submit_survives_http_error(monkeypatch):
    _set_key(monkeypatch)
    respx.post(indexnow.ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))
    out = indexnow.submit_urls(["https://x/a"])  # must not raise
    assert out["submitted"] == 0


def test_keyfile_route_serves_key(client, monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "keyfile789")
    get_settings.cache_clear()
    r = client.get("/keyfile789.txt")
    assert r.status_code == 200 and r.text == "keyfile789"


def test_keyfile_route_404_on_mismatch(client, monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "keyfile789")
    get_settings.cache_clear()
    assert client.get("/somethingelse.txt").status_code == 404


def test_robots_txt_not_shadowed(client, monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "keyfile789")
    get_settings.cache_clear()
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Sitemap:" in r.text
