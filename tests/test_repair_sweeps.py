"""Repair-sweep safety rails: the date-corroboration gate (the LLM invents
calendar dates from bare times) and the OP-comment walker used by the
media-rows archive sweep."""
import backfill_archive_media as bam
from csv_repair import date_corroborated


def test_date_corroborated_accepts_real_dates():
    for t in ["August 31, 2023 8:21 PM", "October 17th/2017 @ 10PM",
              "08/03/2025", "7-11-25 at 10pm", "7/12/25, 12am PDT",
              "January 19th 9:14pm", "July 12th around 1:40pm"]:
        assert date_corroborated(t), t


def test_date_corroborated_rejects_bare_times():
    # '4-730am' is a time range, not April; '23:21' produced 2023-10-01
    for t in ["23:21", "9:33 PM EST.", "4-730am", "12 midnight gmt.",
              "10:00 pm (estimated).", "9:00 PM"]:
        assert not date_corroborated(t), t


def test_date_corroborated_uses_title():
    assert date_corroborated("9:00 PM", "Lake Powell UFO May 31 2024")
    assert not date_corroborated("9:00 PM", "Are these satellites?")


def test_fetch_original_walks_nested_op_comments(monkeypatch):
    children = [
        {"kind": "t1", "data": {"author": "mod", "body": "Time and place?",
         "replies": {"data": {"children": [
             {"kind": "t1", "data": {"author": "witness1", "replies": "",
              "body": "Location: Yakima, WA. 10pm July 11"}}]}}}},
        {"kind": "t1", "data": {"author": "witness1", "body": "[deleted]",
                                "replies": ""}},
        {"kind": "more", "data": {}},
    ]

    class FakeResp:
        status_code = 200

        def json(self):
            return [{"data": {"children": [{"data": {
                        "selftext": "body here", "created_utc": 1752264000}}]}},
                    {"data": {"children": children}}]

    monkeypatch.setattr(bam, "_client", type("C", (), {
        "get": lambda self, *a, **k: FakeResp()})())
    monkeypatch.setattr(bam.time, "sleep", lambda s: None)
    post, ops = bam.fetch_original("tok", "abc123", "witness1")
    assert post["selftext"] == "body here"
    assert ops == ["Location: Yakima, WA. 10pm July 11"]


def test_usable_location_rejects_filler():
    from csv_repair import usable_location
    for loc in ["down", "orange", "reading", "unkown", "Unknown", "n/a", ""]:
        assert not usable_location(loc), loc
    for loc in ["Munich", "Lake Powell, Utah", "Łódź, Poland", "Yuma, AZ",
                "south London", "Virginia"]:
        assert usable_location(loc), loc


def test_page_description_fallback():
    from app.helpers import page_description
    full = {"description": "I saw a thing.", "shape": "", "city": "",
            "country": "", "location_text": "", "sighted_at": "2026-07-01T05:00:00Z"}
    assert page_description(full) == "I saw a thing."
    empty = {"description": "  ", "shape": "orb", "city": "Leeds",
             "country": "United Kingdom", "location_text": "",
             "sighted_at": "2024-11-02T21:00:00Z"}
    out = page_description(empty)
    assert "orb-shaped object over Leeds, United Kingdom on 2024-11-02" in out
    bare = {"description": None, "shape": None, "city": None, "country": None,
            "location_text": "", "sighted_at": "2024-11-02T21:00:00Z"}
    assert page_description(bare).startswith("Eyewitness UFO report on 2024-11-02")
