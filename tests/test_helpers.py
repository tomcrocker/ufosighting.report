from app import helpers


def test_option_lists():
    assert len(helpers.SHAPES) == 22 and "saucer" in helpers.SHAPES
    assert helpers.NUM_OBJECTS == ["1", "2", "3", "4", "5+"]
    assert "as high as a star" in helpers.DISTANCES
    assert "golf ball" in helpers.SIZES
    assert "abrupt changes in direction" in helpers.MOVEMENTS
    assert helpers.FEATURE_ANSWERS == ["yes", "no", "unsure"]
    assert helpers.MIN_STORY_CHARS == 150


def test_slugify():
    assert helpers.slugify("Bright ORB over the lake!!") == "bright-orb-over-the-lake"
    assert helpers.slugify("???") == "sighting"
    assert len(helpers.slugify("x" * 500)) <= 60


def test_humanize_duration():
    assert helpers.humanize_duration(None) == ""
    assert helpers.humanize_duration(1) == "1 second"
    assert helpers.humanize_duration(45) == "45 seconds"
    assert helpers.humanize_duration(120) == "2 minutes"
    assert helpers.humanize_duration(5400) == "1.5 hours"
    assert helpers.humanize_duration(7200) == "2 hours"


def test_to_utc_and_back():
    dt = helpers.to_utc("2026-07-01", "22:15", "America/Vancouver")
    assert dt.strftime(helpers.ISO) == "2026-07-02T05:15:00Z"
    assert helpers.from_utc("2026-07-02T05:15:00Z", "America/Vancouver") == "2026-07-01 22:15"


def _full_clean():
    return {
        "tz_name": "America/Vancouver",
        "description": "A silent orange orb hovered.",
        "num_objects": "2", "shape": "sphere",
        "distance": "above the trees", "apparent_size": "golf ball",
        "movement": ["hovering", "extremely fast"],
        "duration_seconds": 120,
        "has_wings": "no", "has_rotors": None, "has_plume": "unsure", "makes_noise": "yes",
        "witnesses": 2, "sensors": ["infrared"], "witness_background": ["pilot"],
    }


def test_format_post_body_full():
    body = helpers.format_post_body(
        _full_clean(),
        sighted_local="2026-07-01 22:15",
        location_line="Lake Cowichan, BC, Canada",
        media_urls=["https://media.test/uploads/2026/07/aa.jpg"],
        gallery_url="https://ufosighting.report/sighting/1/bright-orb",
    )
    assert "**When:** 2026-07-01 22:15 (America/Vancouver)" in body
    assert "**Where:** Lake Cowichan, BC, Canada" in body
    assert "**Objects:** 2" in body
    assert "**Shape:** sphere" in body
    assert "**Closest distance:** above the trees" in body
    assert "**Apparent size (at arm's length):** golf ball" in body
    assert "**Movement:** hovering, extremely fast" in body
    assert "**Duration:** 2 minutes" in body
    assert "**Features:** wings: no · exhaust plume: unsure · noise: yes" in body
    assert "**Witnesses:** 2" in body
    assert "**Sensor detection:** infrared" in body
    assert "**Reporter background:** pilot" in body
    assert "- https://media.test/uploads/2026/07/aa.jpg" in body
    assert "https://ufosighting.report/sighting/1/bright-orb" in body


def test_format_post_body_skips_empty_fields():
    body = helpers.format_post_body(
        {"tz_name": "UTC", "description": "d"},
        sighted_local="2026-07-01 22:15", location_line="",
        media_urls=[], gallery_url="https://x/1",
    )
    for label in ("**Where:**", "**Objects:**", "**Shape:**", "**Movement:**",
                  "**Features:**", "**Sensor detection:**", "**Media:**"):
        assert label not in body


def test_clean_username():
    assert helpers.clean_username("u/Example_1") == "Example_1"
    assert helpers.clean_username("/u/tmosh") == "tmosh"
    assert helpers.clean_username("  Witness-9 ") == "Witness-9"
    assert helpers.clean_username("ab") is None            # too short
    assert helpers.clean_username("has space") is None
    assert helpers.clean_username("bad!char") is None


def test_format_post_body_attribution():
    body = helpers.format_post_body(
        {"tz_name": "UTC", "description": "d"},
        sighted_local="2026-07-01 22:15", location_line="",
        media_urls=[], gallery_url="https://x/1",
        attribution="Reported by u/witness1 (verified via ufosighting.report)",
    )
    assert "Reported by u/witness1 (verified via ufosighting.report)" in body


def test_haversine_known_distance():
    from app.helpers import haversine_km
    # Victoria BC -> Vancouver BC ≈ 93-94 km
    d = haversine_km(48.4284, -123.3656, 49.2827, -123.1207)
    assert 90 < d < 98
    assert haversine_km(48.0, -123.0, 48.0, -123.0) == 0
