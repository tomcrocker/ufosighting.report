from app import skycontext

COORDS = (48.8123, -124.1456, "2026-07-01T05:30:00Z")


def test_links_none_without_coords():
    assert skycontext.links(None, None, "2026-07-01T05:30:00Z") is None
    assert skycontext.links(48.8, -124.1, "") is None


def test_link_formats():
    out = skycontext.links(*COORDS)
    # tar1090 playback rewinds the area to the exact minute
    assert "replay=2026-07-01-05:30" in out["adsb"]
    # FR24 treats >2 decimals as a flight callsign
    assert out["fr24"] == "https://www.flightradar24.com/48.81,-124.15/9"
    assert "lat=48.8123&lng=-124.1456" in out["heavens"]
    assert "year=2026&month=7&day=1" in out["skychart"]


def test_markdown_empty_without_links():
    assert skycontext.markdown(None) == ""
    assert skycontext.markdown(None, {"checked": True}) == ""


def test_markdown_always_carries_the_four_links():
    md = skycontext.markdown(skycontext.links(*COORDS))
    for label in ("Aircraft that day", "Sky chart for that date",
                  "Satellite passes at this spot", "Live air traffic"):
        assert label in md
    assert "globe.adsbexchange.com" in md and "in-the-sky.org" in md
    assert "heavens-above.com" in md and "flightradar24.com" in md


def test_markdown_all_clear_when_nothing_overhead():
    md = skycontext.markdown(skycontext.links(*COORDS), {
        "checked": True, "catalog_date": "2026-07-01", "bright": [],
        "trains": [], "launches": [], "iss": None, "starlink_visible": 0})
    assert "No bright satellites were visible overhead" in md
    assert "2026-07-01 catalog" in md


def test_markdown_train_supersedes_bright_list():
    md = skycontext.markdown(skycontext.links(*COORDS), {
        "checked": True, "catalog_date": "2026-07-01",
        "trains": [{"count": 23, "az": "NW", "time": "05:28"}],
        "bright": [{"name": "COSMOS 1234", "alt": 40, "az": "SW", "time": "05:31"}],
        "launches": [], "iss": None, "starlink_visible": 23})
    assert "Starlink train overhead" in md and "23 satellites" in md
    # the bright list is suppressed when a train explains the sighting
    assert "COSMOS 1234" not in md


def test_markdown_reports_iss_and_launch():
    md = skycontext.markdown(skycontext.links(*COORDS), {
        "checked": True, "catalog_date": "2026-07-01", "trains": [], "bright": [],
        "iss": {"alt": 62, "az": "SSE", "time": "05:29"},
        "launches": [{"minutes_after": -12, "provider": "SpaceX", "name": "Starlink 9-5",
                      "pad": "Vandenberg SLC-4E", "distance_km": 340}],
        "starlink_visible": 0})
    assert "The ISS was overhead" in md and "62° above the SSE" in md
    assert "Rocket launch 12 min after this sighting" in md
    assert "SpaceX Starlink 9-5" in md


def test_markdown_skips_computed_block_when_unchecked():
    md = skycontext.markdown(skycontext.links(*COORDS), {"checked": False, "reason": "no TLEs"})
    assert "Aircraft that day" in md          # links still useful
    assert "No bright satellites" not in md   # but no claims about the sky
