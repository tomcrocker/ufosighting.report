import sync
from app import reddit
from tests.test_db import _insert_sighting


def _seed(db_conn, post_id, status="live"):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "UPDATE sightings SET reddit_post_id=?, status=? WHERE id=?", (post_id, status, sid)
    )
    db_conn.commit()
    return sid


def _fake_infos(monkeypatch, infos: dict):
    monkeypatch.setattr(
        sync.reddit, "fetch_posts_info",
        lambda post_ids: {pid: infos[pid] for pid in post_ids if pid in infos},
    )
    # keep the piggybacked comment refresh off the network in unit tests
    monkeypatch.setattr(sync.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(sync.comments, "refresh_for_sighting",
                        lambda conn, tok, sid, pid: 0)


def test_removed_post_hides_entry(db_conn, monkeypatch):
    sid = _seed(db_conn, "aaa", "live")
    _fake_infos(monkeypatch, {"aaa": reddit.PostInfo("moderator", 5, 2)})
    result = sync.sync_once(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "removed_on_reddit"
    assert result == {"checked": 1, "updated": 1, "comments": 0}


def test_approved_post_flips_back_to_live(db_conn, monkeypatch):
    sid = _seed(db_conn, "bbb", "removed_on_reddit")
    _fake_infos(monkeypatch, {"bbb": reddit.PostInfo(None, 12, 4)})
    sync.sync_once(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live"
    assert row["reddit_score"] == 12
    assert row["reddit_num_comments"] == 4


def test_author_deleted_post(db_conn, monkeypatch):
    sid = _seed(db_conn, "ccc", "live")
    _fake_infos(monkeypatch, {"ccc": reddit.PostInfo("deleted", 3, 1)})
    sync.sync_once(db_conn)
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "deleted_by_user"


def test_hidden_by_admin_never_touched(db_conn, monkeypatch):
    sid = _seed(db_conn, "ddd", "hidden_by_admin")
    called = []
    monkeypatch.setattr(
        sync.reddit, "fetch_posts_info",
        lambda post_ids: called.extend(post_ids) or {},
    )
    sync.sync_once(db_conn)
    assert "ddd" not in called
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "hidden_by_admin"


def test_pending_post_never_touched(db_conn, monkeypatch):
    _seed(db_conn, "eee", "pending_post")
    _fake_infos(monkeypatch, {})
    assert sync.sync_once(db_conn)["checked"] == 0


def test_score_refresh_counts_as_checked_not_updated(db_conn, monkeypatch):
    _seed(db_conn, "fff", "live")
    _fake_infos(monkeypatch, {"fff": reddit.PostInfo(None, 99, 10)})
    assert sync.sync_once(db_conn) == {"checked": 1, "updated": 0, "comments": 0}


def test_missing_info_is_skipped(db_conn, monkeypatch):
    sid = _seed(db_conn, "ggg", "live")
    _fake_infos(monkeypatch, {})
    result = sync.sync_once(db_conn)
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live"
    assert result == {"checked": 1, "updated": 0, "comments": 0}


def test_main_runs_sweep(db_conn, monkeypatch):
    import sync
    called = {}
    monkeypatch.setattr(sync.verify, "sweep_pending_verify", lambda conn, w: called.setdefault("w", w) or 0)
    monkeypatch.setattr(sync, "sync_once", lambda conn, **kw: {"checked": 0, "updated": 0, "comments": 0})
    monkeypatch.setattr(sync.db, "connect", lambda p: db_conn)
    sync.main()  # closes db_conn; fixture teardown double-close is harmless on sqlite
    assert called["w"] == 6


def test_sync_hot_window_excludes_old_posts(db_conn, monkeypatch):
    old = _seed(db_conn, "old1", "live")
    db_conn.execute("UPDATE sightings SET created_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 days') "
                    "WHERE id=?", (old,))
    _seed(db_conn, "new1", "live")
    db_conn.commit()
    checked = []
    monkeypatch.setattr(sync.reddit, "fetch_posts_info",
                        lambda ids: checked.extend(ids) or {})
    sync.sync_once(db_conn, window_hours=72)
    assert checked == ["new1"]
    checked.clear()
    sync.sync_once(db_conn, window_hours=30 * 24)
    assert sorted(checked) == ["new1", "old1"]


def test_sync_refreshes_comments_for_live_only(db_conn, monkeypatch):
    _seed(db_conn, "aaa", "live")
    _seed(db_conn, "bbb", "live")  # becomes removed during this sync
    _fake_infos(monkeypatch, {"aaa": reddit.PostInfo(None, 5, 2),
                              "bbb": reddit.PostInfo("moderator", 1, 0)})
    refreshed = []
    monkeypatch.setattr(sync.comments, "refresh_for_sighting",
                        lambda conn, tok, sid, pid: refreshed.append(pid) or 3)
    result = sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert refreshed == ["aaa"]
    assert result["comments"] == 1


def test_sync_survives_token_failure_in_comment_pass(db_conn, monkeypatch):
    _seed(db_conn, "aaa", "live")
    _fake_infos(monkeypatch, {"aaa": reddit.PostInfo(None, 5, 2)})

    def no_token():
        raise sync.reddit.RedditError("script token failed: HTTP 200")

    monkeypatch.setattr(sync.reddit, "script_token", no_token)
    result = sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert result == {"checked": 1, "updated": 0, "comments": 0}
