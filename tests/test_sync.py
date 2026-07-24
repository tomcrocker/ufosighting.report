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


def test_mod_removed_post_is_hidden(db_conn, monkeypatch):
    # a real mod pulled it -> removed_by_mod (hidden), and the reason is stored
    sid = _seed(db_conn, "aaa", "live")
    _fake_infos(monkeypatch, {"aaa": reddit.PostInfo("moderator", 5, 2)})
    result = sync.sync_once(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "removed_by_mod"
    assert row["removed_by_category"] == "moderator"
    assert result == {"checked": 1, "updated": 1, "comments": 0}


def test_spam_filtered_post_stays_visible(db_conn, monkeypatch):
    # Reddit spam-filter / modqueue-pending -> removed_on_reddit, still visible,
    # since a mod may yet approve it
    sid = _seed(db_conn, "aaz", "live")
    _fake_infos(monkeypatch, {"aaz": reddit.PostInfo("automod_filtered", 5, 2)})
    sync.sync_once(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "removed_on_reddit"
    assert row["removed_by_category"] == "automod_filtered"


def test_mod_removed_post_can_be_reapproved(db_conn, monkeypatch):
    # sync must keep re-checking removed_by_mod so a re-approval returns to live
    sid = _seed(db_conn, "aay", "removed_by_mod")
    _fake_infos(monkeypatch, {"aay": reddit.PostInfo(None, 9, 3)})
    sync.sync_once(db_conn)
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live"


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


def _seed_site(db_conn, post_id, status="live"):
    sid = _seed(db_conn, post_id, status)
    db_conn.execute("UPDATE sightings SET source='site' WHERE id=?", (sid,))
    db_conn.commit()
    return sid


def test_sync_self_approves_spam_removed_bot_post(db_conn, monkeypatch):
    sid = _seed_site(db_conn, "bot1")
    _fake_infos(monkeypatch, {"bot1": reddit.PostInfo("reddit", 5, 2)})
    approved = []
    monkeypatch.setattr(sync.reddit, "approve",
                        lambda tok, *, post_id: approved.append(post_id))
    sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert approved == ["bot1"]
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live"  # rescued, not flipped to removed


def test_sync_respects_mod_removal_of_bot_post(db_conn, monkeypatch):
    sid = _seed_site(db_conn, "bot2")
    _fake_infos(monkeypatch, {"bot2": reddit.PostInfo("moderator", 5, 2)})
    approved = []
    monkeypatch.setattr(sync.reddit, "approve",
                        lambda tok, *, post_id: approved.append(post_id))
    sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert not approved
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "removed_by_mod"


def test_sync_never_approves_ingested_posts(db_conn, monkeypatch):
    sid = _seed(db_conn, "ing1")
    db_conn.execute("UPDATE sightings SET source='reddit' WHERE id=?", (sid,))
    db_conn.commit()
    _fake_infos(monkeypatch, {"ing1": reddit.PostInfo("reddit", 5, 2)})
    approved = []
    monkeypatch.setattr(sync.reddit, "approve",
                        lambda tok, *, post_id: approved.append(post_id))
    sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert not approved


def test_sync_rescues_removed_bot_comment(db_conn, monkeypatch):
    sid = _seed_site(db_conn, "bot3")
    _fake_infos(monkeypatch, {"bot3": reddit.PostInfo(None, 5, 2)})
    monkeypatch.setattr(sync.reddit, "fetch_removed_bot_comments",
                        lambda tok, pid, bot: ["cmtX"] if pid == "bot3" else [])
    approved = []
    monkeypatch.setattr(sync.reddit, "approve",
                        lambda tok, *, post_id=None, comment_id=None:
                        approved.append(comment_id or post_id))
    sync.sync_once(db_conn, comment_sleep=lambda s: None)
    assert "cmtX" in approved


LIKELY_ID = "4288aad0-c903-11eb-8d14-0e4c1f8ec6d1"


def test_likely_identified_flair_synced(db_conn, monkeypatch):
    sid = _seed(db_conn, "lid1", "live")
    _fake_infos(monkeypatch, {"lid1": reddit.PostInfo(None, 8, 3, flair_template_id=LIKELY_ID)})
    sync.sync_once(db_conn)
    assert db_conn.execute("SELECT likely_identified FROM sightings WHERE id=?",
                           (sid,)).fetchone()[0] == 1


def test_likely_identified_cleared_when_reflaired(db_conn, monkeypatch):
    sid = _seed(db_conn, "lid2", "live")
    db_conn.execute("UPDATE sightings SET likely_identified=1 WHERE id=?", (sid,))
    db_conn.commit()
    # mods flair it back to the Sighting flair -> the flag clears
    _fake_infos(monkeypatch, {"lid2": reddit.PostInfo(None, 8, 3, flair_template_id="de39d1a0")})
    sync.sync_once(db_conn)
    assert db_conn.execute("SELECT likely_identified FROM sightings WHERE id=?",
                           (sid,)).fetchone()[0] == 0


def test_no_flair_stays_not_identified(db_conn, monkeypatch):
    sid = _seed(db_conn, "lid3", "live")
    _fake_infos(monkeypatch, {"lid3": reddit.PostInfo(None, 8, 3)})
    sync.sync_once(db_conn)
    assert db_conn.execute("SELECT likely_identified FROM sightings WHERE id=?",
                           (sid,)).fetchone()[0] == 0
