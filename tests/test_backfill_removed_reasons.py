import backfill_removed_reasons as brr
from app import reddit
from tests.test_db import _insert_sighting


def _seed(conn, pid, status="removed_on_reddit"):
    sid = _insert_sighting(conn)
    conn.execute("UPDATE sightings SET reddit_post_id=?, status=? WHERE id=?",
                 (pid, status, sid))
    conn.commit()
    return sid


def _row(conn, sid):
    return conn.execute(
        "SELECT status, removed_by_category FROM sightings WHERE id=?", (sid,)).fetchone()


def test_reclassify_splits_by_real_reason(db_conn, monkeypatch):
    mod = _seed(db_conn, "m1")        # real mod removal -> hidden
    spam = _seed(db_conn, "s1")       # spam-filter/pending -> stays visible
    back = _seed(db_conn, "b1")       # approved since -> resurrected to live

    def fake(ids):
        m = {"m1": reddit.PostInfo("moderator", 1, 0),
             "s1": reddit.PostInfo("automod_filtered", 1, 0),
             "b1": reddit.PostInfo(None, 1, 0)}
        return {i: m[i] for i in ids if i in m}

    monkeypatch.setattr(reddit, "fetch_posts_info", fake)
    result = brr.reclassify(db_conn, sleep=lambda *_: None)

    assert _row(db_conn, mod)["status"] == "removed_by_mod"
    assert _row(db_conn, mod)["removed_by_category"] == "moderator"
    assert _row(db_conn, spam)["status"] == "removed_on_reddit"
    assert _row(db_conn, spam)["removed_by_category"] == "automod_filtered"
    assert _row(db_conn, back)["status"] == "live"
    assert result["total"] == 3


def test_reclassify_retries_on_429(db_conn, monkeypatch):
    _seed(db_conn, "r1")
    calls = {"n": 0}

    def flaky(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise reddit.RedditError("info fetch failed: HTTP 429")
        return {"r1": reddit.PostInfo("moderator", 1, 0)}

    monkeypatch.setattr(reddit, "fetch_posts_info", flaky)
    slept = []
    brr.reclassify(db_conn, sleep=slept.append)
    assert calls["n"] == 2 and 30 in slept
