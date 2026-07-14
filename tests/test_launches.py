"""Launch-plume matching + ISS callout extraction."""
from app import launches, satellites

VANDENBERG = {"id": "x1", "name": "Starlink Group 9-1", "provider": "SpaceX",
              "net": "2024-06-19T03:40:00Z", "pad": "Vandenberg SFB, CA, USA",
              "lat": 34.632, "lon": -120.611}


def _prime(monkeypatch, items):
    monkeypatch.setattr(launches, "_cache", items)


def test_launch_matches_within_window_and_range(monkeypatch):
    _prime(monkeypatch, [VANDENBERG])
    # Phoenix AZ (~800 km from Vandenberg), 25 min after liftoff
    hits = launches.matches(33.45, -112.07, "2024-06-19T04:05:00Z")
    assert len(hits) == 1
    h = hits[0]
    assert h["provider"] == "SpaceX" and h["minutes_after"] == 25
    assert 700 < h["distance_km"] < 900


def test_launch_rejected_outside_window_or_range(monkeypatch):
    _prime(monkeypatch, [VANDENBERG])
    # 3 hours later: plume long gone
    assert launches.matches(33.45, -112.07, "2024-06-19T06:40:00Z") == []
    # New York: way beyond plume visibility
    assert launches.matches(40.71, -74.0, "2024-06-19T04:05:00Z") == []


def test_launch_shortly_before_liftoff_still_matches(monkeypatch):
    # reporters sometimes note the time a few minutes early
    _prime(monkeypatch, [VANDENBERG])
    hits = launches.matches(34.0, -118.2, "2024-06-19T03:35:00Z")
    assert len(hits) == 1 and hits[0]["minutes_after"] == -5


def test_extract_iss_pulls_station_out_of_bright_list():
    bright = [{"name": "COSMOS 2251", "alt": 40, "az": "N", "time": "04:00"},
              {"name": "ISS (ZARYA)", "alt": 62, "az": "SW", "time": "04:02"}]
    iss, rest = satellites.extract_iss(bright)
    assert iss["name"] == "ISS (ZARYA)"
    assert [b["name"] for b in rest] == ["COSMOS 2251"]


def test_extract_iss_none_when_absent():
    bright = [{"name": "TIANGONG", "alt": 30, "az": "S", "time": "04:00"}]
    iss, rest = satellites.extract_iss(bright)
    assert iss is None and rest == bright
