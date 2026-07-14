import os
from datetime import datetime, timezone

from app import satellites

# A real ISS TLE (epoch 2024-01-01ish) — position math is deterministic
ISS_TLE = """ISS (ZARYA)
1 25544U 98067A   24001.50000000  .00016717  00000+0  30777-3 0  9993
2 25544  51.6416 190.0000 0005000  50.0000 310.0000 15.49500000430000
"""


def _write_catalog(tmp_path, monkeypatch, date="2024-01-01"):
    tle_dir = tmp_path / "tle"
    tle_dir.mkdir()
    (tle_dir / f"stations-{date}.tle").write_text(ISS_TLE)
    monkeypatch.setattr(satellites, "TLE_DIR", str(tle_dir))
    return tle_dir


def test_nearest_catalog_respects_age_limit(tmp_path, monkeypatch):
    _write_catalog(tmp_path, monkeypatch, "2024-01-01")
    assert satellites._nearest_catalog_date("2024-01-05") == "2024-01-01"
    assert satellites._nearest_catalog_date("2024-02-20") is None


def test_passes_for_unchecked_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(satellites, "TLE_DIR", str(tmp_path / "empty"))
    out = satellites.passes_for(48.4, -123.3, "2026-07-01T06:00:00Z")
    assert out["checked"] is False
    assert "orbital data" in out["reason"]


def test_passes_for_detects_overhead_sat(tmp_path, monkeypatch):
    _write_catalog(tmp_path, monkeypatch, "2024-01-01")
    monkeypatch.setattr(satellites, "_ephemeris", lambda: None)  # no 17MB download
    # place the observer directly under the ISS at its TLE epoch: guaranteed pass
    from skyfield.api import EarthSatellite, load, wgs84
    ts = load.timescale()
    lines = ISS_TLE.strip().splitlines()
    sat = EarthSatellite(lines[1], lines[2], lines[0], ts)
    epoch_dt = sat.epoch.utc_datetime()
    sub = wgs84.subpoint_of(sat.at(sat.epoch))
    when = epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = satellites.passes_for(sub.latitude.degrees, sub.longitude.degrees, when)
    assert out["checked"] is True
    assert out["visibility_filtered"] is False
    # the ISS is pulled out of the generic bright list into its own callout
    assert out["iss"] is not None and out["iss"]["name"] == "ISS (ZARYA)"
    assert out["iss"]["alt"] > 80  # directly underneath
    assert out["iss"]["az"]  # compass name present
    assert all(b["name"] != "ISS (ZARYA)" for b in out["bright"])


def test_fetch_today_caches(tmp_path, monkeypatch):
    import httpx

    class FakeResp:
        status_code = 200
        text = ISS_TLE.replace("ISS (ZARYA)", "1 FAKE")  # starts with '1 '

    calls = []
    monkeypatch.setattr(satellites, "TLE_DIR", str(tmp_path / "tle"))
    monkeypatch.setattr(httpx, "get", lambda url, **k: calls.append(url) or FakeResp())
    written = satellites.fetch_today(groups=("stations",))
    assert len(written) == 1 and os.path.exists(written[0])
    # second call: cached, no fetch
    assert satellites.fetch_today(groups=("stations",)) == []
    assert len(calls) == 1
