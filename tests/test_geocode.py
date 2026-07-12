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


@respx.mock
def test_reverse_returns_nearest_town(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get("https://nominatim.openstreetmap.org/reverse").mock(
        return_value=httpx.Response(200, json={
            "display_name": "Saanich, Capital Regional District, British Columbia, Canada",
            "lat": "48.45", "lon": "-123.37",
            "addresstype": "town",
            "address": {"town": "Saanich", "state": "British Columbia",
                        "country": "Canada"},
        }))
    out = geocode.reverse(48.46, -123.38)
    assert out["city"] == "Saanich" and out["country"] == "Canada"
    # short label: nearest town + region + country, not a full street address
    assert out["label"] == "Saanich, British Columbia, Canada"


@respx.mock
def test_reverse_none_on_failure(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get("https://nominatim.openstreetmap.org/reverse").mock(
        return_value=httpx.Response(500))
    assert geocode.reverse(0, 0) is None


@respx.mock
def test_search_includes_addresstype(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get("https://nominatim.openstreetmap.org/search").mock(
        return_value=httpx.Response(200, json=[
            {"display_name": "Canada", "lat": "61", "lon": "-98",
             "addresstype": "country", "address": {"country": "Canada"}},
        ]))
    out = geocode.search("canada")
    assert out[0]["addresstype"] == "country"


# --- /api endpoints (client fixture) ---

@respx.mock
def test_api_reverse_endpoint(client, monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get("https://nominatim.openstreetmap.org/reverse").mock(
        return_value=httpx.Response(200, json={
            "display_name": "Saanich, CRD, BC, Canada", "lat": "48.45", "lon": "-123.37",
            "addresstype": "town",
            "address": {"town": "Saanich", "state": "British Columbia", "country": "Canada"},
        }))
    r = client.get("/api/reverse?lat=48.46&lon=-123.38")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "Saanich, British Columbia, Canada"
    assert body["city"] == "Saanich"


def test_api_reverse_validates_coords(client):
    assert client.get("/api/reverse?lat=999&lon=0").status_code == 400


@respx.mock
def test_api_geocode_filters_countries(client, monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get("https://nominatim.openstreetmap.org/search").mock(
        return_value=httpx.Response(200, json=[
            {"display_name": "Canada", "lat": "61", "lon": "-98",
             "addresstype": "country", "address": {"country": "Canada"}},
            {"display_name": "Canada Bay, Sydney, Australia", "lat": "-33.86", "lon": "151.11",
             "addresstype": "suburb", "address": {"city": "Sydney", "country": "Australia"}},
        ]))
    r = client.get("/api/geocode?q=canada")
    names = [x["display_name"] for x in r.json()["results"]]
    assert "Canada" not in names and len(names) == 1


# --- candidate ladder for fuzzy/verbose location strings ---

def test_candidates_strips_parentheticals():
    out = geocode.candidates("Jeannette, PA (an hour outside Pittsburgh)")
    assert out[0] == "Jeannette, PA"


def test_candidates_handles_near_prefixes():
    out = geocode.candidates("Ontario, near Thunder Bay")
    assert "Thunder Bay, Ontario" in out or "Ontario, Thunder Bay" in out


def test_candidates_drops_leading_descriptor():
    out = geocode.candidates("Toledo Express Airport, Holland Ohio")
    assert "Holland Ohio" in out


def test_candidates_uses_city_country():
    out = geocode.candidates("Sweetwater County heights", city="Rock Springs",
                             country="United States")
    assert "Rock Springs, United States" in out


def test_candidates_never_country_only():
    out = geocode.candidates("", city="", country="Germany")
    assert out == []


def test_candidates_dedupes_and_caps():
    out = geocode.candidates("Paris, France (Paris)", city="Paris", country="France")
    assert len(out) == len(set(out)) and len(out) <= 5


@respx.mock
def test_reverse_retries_finer_zoom_for_city(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    calls = []

    def responder(request):
        calls.append(dict(request.url.params))
        if request.url.params["zoom"] == "10":
            return httpx.Response(200, json={
                "display_name": "British Columbia, Canada", "lat": "52", "lon": "-125",
                "addresstype": "state",
                "address": {"state": "British Columbia", "country": "Canada"}})
        return httpx.Response(200, json={
            "display_name": "Tatla Lake, BC, Canada", "lat": "52", "lon": "-124.6",
            "addresstype": "village",
            "address": {"village": "Tatla Lake", "state": "British Columbia",
                        "country": "Canada"}})

    respx.get("https://nominatim.openstreetmap.org/reverse").mock(side_effect=responder)
    out = geocode.reverse(52.0, -125.0)
    assert out["city"] == "Tatla Lake"
    assert [c["zoom"] for c in calls] == ["10", "12"]


@respx.mock
def test_reverse_country_only_returns_none(monkeypatch):
    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    respx.get("https://nominatim.openstreetmap.org/reverse").mock(
        return_value=httpx.Response(200, json={
            "display_name": "Algeria", "lat": "28", "lon": "2",
            "addresstype": "country", "address": {"country": "Algeria"}}))
    assert geocode.reverse(28.0, 2.0) is None
