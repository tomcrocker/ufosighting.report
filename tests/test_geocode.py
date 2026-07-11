import httpx
import respx

from app import geocode

NOM = "https://nominatim.openstreetmap.org/search"


def _hit():
    return httpx.Response(200, json=[{
        "display_name": "Tofino, BC, Canada", "lat": "49.153", "lon": "-125.905",
        "address": {"town": "Tofino", "country": "Canada"}}])


@respx.mock
def test_search_parses(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get(NOM).mock(return_value=_hit())
    out = geocode.search("Tofino")
    assert out[0]["city"] == "Tofino" and out[0]["country"] == "Canada"
    assert abs(out[0]["lat"] - 49.153) < 1e-6


@respx.mock
def test_forward_best_match_and_caches(db_conn, monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    route = respx.get(NOM).mock(return_value=_hit())
    r1 = geocode.forward(db_conn, "Tofino BC")
    assert r1["lat"] and r1["city"] == "Tofino"
    # cached: second call must NOT hit the network
    r2 = geocode.forward(db_conn, "Tofino BC")
    assert r2["city"] == "Tofino"
    assert route.call_count == 1


@respx.mock
def test_forward_no_result_returns_none(db_conn, monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get(NOM).mock(return_value=httpx.Response(200, json=[]))
    assert geocode.forward(db_conn, "asdfqwer nowhere") is None
