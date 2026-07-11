import ingest


def _post(pid="p1", **over):
    d = {"id": pid, "title": "Orb over town", "author": "witness9",
         "selftext": "Saw an orb.", "created_utc": 1751000000,
         "permalink": f"/r/UFOs/comments/{pid}/x/", "url": "https://reddit.com/x",
         "link_flair_text": "Sighting", "is_self": True}
    d.update(over)
    return d


def test_ingest_creates_reddit_source_entry(db_conn, monkeypatch):
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    assert ingest.ingest_post(db_conn, _post()) is True
    row = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='p1'").fetchone()
    assert row["source"] == "reddit" and row["status"] == "live"
    assert row["reddit_username"] == "witness9"


def test_ingest_dedup(db_conn, monkeypatch):
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    ingest.ingest_post(db_conn, _post())
    assert ingest.ingest_post(db_conn, _post()) is False  # already present
    assert db_conn.execute(
        "SELECT COUNT(*) FROM sightings WHERE reddit_post_id='p1'").fetchone()[0] == 1


def test_ingest_once_uses_listing(db_conn, monkeypatch):
    monkeypatch.setattr(ingest.reddit, "script_token", lambda: "t")
    monkeypatch.setattr(ingest.reddit, "list_flair_posts",
                        lambda tok, **k: ([_post("a"), _post("b")], None))
    monkeypatch.setattr(ingest, "download_media", lambda post: [])
    res = ingest.ingest_once(db_conn)
    assert res == {"seen": 2, "added": 2}


def test_ingest_media_failure_non_fatal(db_conn, monkeypatch):
    def boom(post):
        raise RuntimeError("net")
    monkeypatch.setattr(ingest, "download_media", boom)
    assert ingest.ingest_post(db_conn, _post()) is True  # entry still created
    assert db_conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_ingest_media_attached(db_conn, monkeypatch):
    monkeypatch.setattr(ingest, "download_media",
                        lambda post: [(b"jpegbytes", "image/jpeg", ".jpg")])
    monkeypatch.setattr(ingest.r2, "put_bytes", lambda k, d, ct: None)
    ingest.ingest_post(db_conn, _post())
    row = db_conn.execute("SELECT id FROM sightings WHERE reddit_post_id='p1'").fetchone()
    n = db_conn.execute("SELECT COUNT(*) FROM media WHERE sighting_id=?", (row["id"],)).fetchone()[0]
    assert n == 1
