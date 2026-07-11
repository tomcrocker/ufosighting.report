import scan_youtube


def _mk_sighting(conn, desc="", pid=None, with_media=False, source="reddit"):
    conn.execute("INSERT INTO sightings (source, reddit_username, title, description, "
                 "sighted_at, reddit_post_id, status) VALUES "
                 "(?,'u','t',?,'2026-01-01T00:00:00Z',?,'live')", (source, desc, pid))
    sid = conn.execute("SELECT MAX(id) FROM sightings").fetchone()[0]
    if with_media:
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind) "
                     "VALUES (?,'uploads/x.mp4','video')", (sid,))
    return sid


def test_body_hit_no_api_call(db_conn):
    sid = _mk_sighting(db_conn, desc="see https://youtu.be/XHWPQEJ_TVA", pid="aaa")
    calls = []
    stats = scan_youtube.scan(db_conn, fetch=lambda p: calls.append(p),
                              sleep=lambda s: None)
    assert stats == {"scanned": 1, "body_hits": 1, "api_hits": 0, "enqueued": 1}
    assert calls == []
    job = db_conn.execute("SELECT * FROM yt_jobs").fetchone()
    assert job["sighting_id"] == sid
    assert job["url"] == "https://www.youtube.com/watch?v=XHWPQEJ_TVA"


def test_api_fallback_for_link_post(db_conn):
    _mk_sighting(db_conn, desc="", pid="bbb")
    stats = scan_youtube.scan(
        db_conn, fetch=lambda p: {"url": "https://youtu.be/XHWPQEJ_TVA", "selftext": ""},
        sleep=lambda s: None)
    assert stats["api_hits"] == 1 and stats["enqueued"] == 1


def test_api_fetch_none_is_skipped(db_conn):
    _mk_sighting(db_conn, desc="", pid="gone")
    stats = scan_youtube.scan(db_conn, fetch=lambda p: None, sleep=lambda s: None)
    assert stats == {"scanned": 1, "body_hits": 0, "api_hits": 0, "enqueued": 0}


def test_skips_rows_with_media_jobs_or_site_source(db_conn):
    _mk_sighting(db_conn, desc="https://youtu.be/XHWPQEJ_TVA", pid="ccc", with_media=True)
    sid = _mk_sighting(db_conn, desc="https://youtu.be/XHWPQEJ_TVA", pid="ddd")
    db_conn.execute("INSERT INTO yt_jobs (sighting_id, url) VALUES (?,'x')", (sid,))
    _mk_sighting(db_conn, desc="https://youtu.be/XHWPQEJ_TVA", source="site")
    stats = scan_youtube.scan(db_conn, fetch=lambda p: None, sleep=lambda s: None)
    assert stats == {"scanned": 0, "body_hits": 0, "api_hits": 0, "enqueued": 0}
