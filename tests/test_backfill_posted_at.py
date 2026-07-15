import backfill_posted_at
from app import reddit


def _seed(conn, **over):
    row = {"reddit_username": "w", "title": "t", "description": "d",
           "sighted_at": "2026-07-01T05:00:00Z", "tz_name": "UTC",
           "location_text": "x", "status": "live"}
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    cur = conn.execute(f"INSERT INTO sightings ({cols}) VALUES ({marks})",
                       list(row.values()))
    conn.commit()
    return cur.lastrowid


def _posted_at(conn, sid):
    return conn.execute("SELECT reddit_posted_at FROM sightings WHERE id=?",
                        (sid,)).fetchone()[0]


def test_backfill_populates_only_missing(db_conn, monkeypatch):
    need = _seed(db_conn, reddit_post_id="pid_need")
    already = _seed(db_conn, reddit_post_id="pid_have",
                    reddit_posted_at="2020-01-01T00:00:00Z")
    no_post = _seed(db_conn)  # no reddit_post_id -> not a candidate

    def fake_info(ids):
        assert ids == ["pid_need"]  # only the un-dated post is queried
        return {"pid_need": reddit.PostInfo(None, 10, 3, created_utc=1784139840)}

    monkeypatch.setattr(reddit, "fetch_posts_info", fake_info)
    result = backfill_posted_at.backfill(db_conn, sleep=lambda *_: None)

    assert result == {"candidates": 1, "updated": 1}
    assert _posted_at(db_conn, need) == "2026-07-15T18:24:00Z"
    # existing value untouched, no-post row still null
    assert _posted_at(db_conn, already) == "2020-01-01T00:00:00Z"
    assert _posted_at(db_conn, no_post) is None


def test_backfill_retries_on_429(db_conn, monkeypatch):
    sid = _seed(db_conn, reddit_post_id="p429")
    calls = {"n": 0}

    def flaky(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise reddit.RedditError("info fetch failed: HTTP 429")
        return {"p429": reddit.PostInfo(None, 1, 1, created_utc=1784139840)}

    monkeypatch.setattr(reddit, "fetch_posts_info", flaky)
    slept = []
    result = backfill_posted_at.backfill(db_conn, sleep=slept.append)
    assert calls["n"] == 2  # retried once after the 429
    assert result == {"candidates": 1, "updated": 1}
    assert _posted_at(db_conn, sid) == "2026-07-15T18:24:00Z"
    assert 30 in slept  # backed off before retrying


def test_backfill_reraises_non_rate_limit(db_conn, monkeypatch):
    import pytest
    _seed(db_conn, reddit_post_id="pboom")
    monkeypatch.setattr(reddit, "fetch_posts_info",
                        lambda ids: (_ for _ in ()).throw(
                            reddit.RedditError("info fetch failed: HTTP 500")))
    with pytest.raises(reddit.RedditError):
        backfill_posted_at.backfill(db_conn, sleep=lambda *_: None)


def test_backfill_skips_posts_without_created_utc(db_conn, monkeypatch):
    sid = _seed(db_conn, reddit_post_id="gone")
    # removed / very old posts can come back from /api/info without created_utc
    monkeypatch.setattr(
        reddit, "fetch_posts_info",
        lambda ids: {"gone": reddit.PostInfo(None, 0, 0, created_utc=None)})
    result = backfill_posted_at.backfill(db_conn, sleep=lambda *_: None)
    assert result == {"candidates": 1, "updated": 0}
    assert _posted_at(db_conn, sid) is None
