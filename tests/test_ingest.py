import ingest


def _post(pid="p1", **over):
    d = {"id": pid, "title": "Orb over Tofino", "author": "witness9",
         "selftext": "Saw an amber orb at dusk near the inlet.", "created_utc": 1751000000,
         "permalink": f"/r/UFOs/comments/{pid}/x/", "url": "https://reddit.com/x",
         "link_flair_text": "Sighting", "is_self": True}
    d.update(over)
    return d


def _empty_clamped():
    return {k: None for k in ("date", "time", "timezone", "location_text", "city",
                              "country", "shape", "num_objects", "duration_seconds", "summary")}


def _stub_pipeline(monkeypatch, *, clamped=None, coords=None, comments=None):
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    monkeypatch.setattr(ingest, "fetch_op_comments", lambda token, post: comments or [])
    monkeypatch.setattr(ingest.extract, "extract_fields", lambda text: {})
    monkeypatch.setattr(ingest.extract, "validate_and_clamp",
                        lambda raw, post_created_iso: clamped or _empty_clamped())
    monkeypatch.setattr(ingest.geocode, "forward", lambda conn, q: coords)


def test_build_sighted_at_with_date_time_tz():
    c = _empty_clamped(); c.update(date="2026-07-01", time="22:15", timezone="America/Vancouver")
    iso, tz = ingest.build_sighted_at(c, "2026-07-05T00:00:00Z")
    assert iso == "2026-07-02T05:15:00Z" and tz == "America/Vancouver"


def test_build_sighted_at_no_time_uses_noon():
    c = _empty_clamped(); c.update(date="2026-07-01", timezone="America/Vancouver")
    iso, tz = ingest.build_sighted_at(c, "2026-07-05T00:00:00Z")
    assert iso == "2026-07-01T19:00:00Z"  # noon PDT = 19:00 UTC


def test_build_sighted_at_no_date_uses_post_time():
    iso, tz = ingest.build_sighted_at(_empty_clamped(), "2026-07-05T00:00:00Z")
    assert iso == "2026-07-05T00:00:00Z" and tz == "UTC"


def test_ingest_post_populates_extracted_fields(db_conn, monkeypatch):
    clamped = _empty_clamped()
    clamped.update(date="2026-07-01", time="22:15", timezone="America/Vancouver",
                   location_text="Tofino, BC", shape="sphere", num_objects="2",
                   duration_seconds=120)
    _stub_pipeline(monkeypatch, clamped=clamped,
                   coords={"lat": 49.15, "lon": -125.9, "city": "Tofino", "country": "Canada",
                           "display_name": "Tofino, BC, Canada"})
    assert ingest.ingest_post(db_conn, _post(), token="t") is True
    row = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='p1'").fetchone()
    assert row["source"] == "reddit" and row["status"] == "live"
    assert row["reddit_username"] == "witness9"
    assert row["shape"] == "sphere" and row["num_objects"] == "2"
    assert row["lat"] == 49.15 and row["country"] == "Canada"
    assert row["sighted_at"] == "2026-07-02T05:15:00Z"


def test_ingest_post_best_effort_when_empty(db_conn, monkeypatch):
    _stub_pipeline(monkeypatch)  # empty clamp, no coords
    assert ingest.ingest_post(db_conn, _post(), token="t") is True
    row = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='p1'").fetchone()
    assert row["lat"] is None and row["shape"] is None
    # sighted_at falls back to post created time (created_utc 1751000000 -> 2025)
    assert row["sighted_at"].startswith("2025-")


def test_ingest_dedup(db_conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    ingest.ingest_post(db_conn, _post(), token="t")
    assert ingest.ingest_post(db_conn, _post(), token="t") is False
    assert db_conn.execute("SELECT COUNT(*) FROM sightings WHERE reddit_post_id='p1'").fetchone()[0] == 1


def test_ingest_once_uses_ingest_subreddit(db_conn, monkeypatch):
    seen = {}
    monkeypatch.setattr(ingest.reddit, "script_token", lambda: "t")

    def fake_list(tok, **k):
        seen.update(k)
        return ([_post("a")], None)

    monkeypatch.setattr(ingest.reddit, "list_flair_posts", fake_list)
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    ingest.ingest_once(db_conn)
    # INGEST_SUBREDDIT unset in tests => falls back to SUBREDDIT (UFOs_sandbox from conftest)
    assert seen["subreddit"] == "UFOs_sandbox"
    assert seen["flair"] == "Sighting"
