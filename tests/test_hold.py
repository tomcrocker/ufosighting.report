from app import appsettings, posting
from tests.test_public import seed


def _queued(app_db, **over):
    sid = seed(app_db, status="pending_post", reddit_username="witness1", **over)
    app_db.execute("UPDATE sightings SET username_verified=1, "
                   "pending_post_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (sid,))
    app_db.commit()
    return sid


def test_hold_diverts_queue_to_review_instead_of_posting(app_db, monkeypatch):
    posted = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: posted.append(sid))
    sid = _queued(app_db)
    appsettings.set(app_db, appsettings.HOLD_POSTS, "1")
    assert posting.process_post_queue(app_db) == 0
    assert posted == []                                   # nothing posted
    status = app_db.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()[0]
    assert status == "pending_review"                     # waits for a human


def test_turning_hold_off_resumes_posting(app_db, monkeypatch):
    posted = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: posted.append(sid))
    sid = _queued(app_db)
    appsettings.set(app_db, appsettings.HOLD_POSTS, "1")
    posting.process_post_queue(app_db)
    # a NEW submission after the hold is lifted posts normally
    appsettings.set(app_db, appsettings.HOLD_POSTS, "0")
    sid2 = _queued(app_db)
    assert posting.process_post_queue(app_db) == 1
    assert posted == [sid2]
    # the one held earlier stays in review for manual approval
    assert app_db.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()[0] \
        == "pending_review"


def test_hold_defaults_off(app_db):
    assert appsettings.hold_posts(app_db) is False


def test_setting_roundtrip_and_update(app_db):
    appsettings.set(app_db, "k", "1")
    assert appsettings.get(app_db, "k") == "1"
    appsettings.set(app_db, "k", "0")           # upsert, not duplicate
    assert appsettings.get(app_db, "k") == "0"
    assert app_db.execute("SELECT COUNT(*) FROM app_settings WHERE key='k'").fetchone()[0] == 1


def test_toggle_route_requires_admin_and_csrf(client, app_db):
    from app import auth
    # anonymous -> the whole admin surface 404s
    assert client.post("/admin/hold-posts", data={"on": "1"}).status_code == 404
    sid = auth.create_session(app_db, "tmosh", "tok", 3600)
    client.cookies.set("sid", sid)
    assert client.post("/admin/hold-posts",
                       data={"on": "1", "csrf_token": "wrong"}).status_code == 403
    assert appsettings.hold_posts(app_db) is False
    r = client.post("/admin/hold-posts",
                    data={"on": "1", "csrf_token": auth.csrf_for(sid)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert appsettings.hold_posts(app_db) is True


def test_approve_credits_verified_when_reporter_confirmed(client, app_db, monkeypatch):
    """A held submission that confirmed its account posts as verified;
    a lapsed-window one stays self-reported."""
    from app import auth, posting
    calls = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: calls.append((sid, verified)))
    confirmed = seed(app_db, status="pending_review", reddit_username="a")
    app_db.execute("UPDATE sightings SET username_verified=1 WHERE id=?", (confirmed,))
    lapsed = seed(app_db, status="pending_review", reddit_username="b")
    app_db.commit()
    sid = auth.create_session(app_db, "tmosh", "tok", 3600)
    client.cookies.set("sid", sid)
    tok = auth.csrf_for(sid)
    for x in (confirmed, lapsed):
        client.post(f"/admin/review/{x}/approve", data={"csrf_token": tok},
                    follow_redirects=False)
    assert (confirmed, True) in calls and (lapsed, False) in calls
