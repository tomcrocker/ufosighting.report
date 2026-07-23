from app import auth
from tests.test_public import seed


def _admin(client, app_db):
    sid = auth.create_session(app_db, "tmosh", "tok-admin", 3600)
    client.cookies.set("sid", sid)
    return sid


def test_admin_404_for_anonymous(client):
    assert client.get("/admin").status_code == 404


def test_admin_404_for_regular_user(logged_in):
    assert logged_in.get("/admin").status_code == 404


def test_admin_page_lists_hidden(client, app_db):
    seed(app_db, title="Hidden entry", status="hidden_by_admin")
    _admin(client, app_db)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Hidden entry" in r.text


def test_hide_and_unhide_action(client, app_db):
    sighting_id = seed(app_db)
    sid = _admin(client, app_db)
    r = client.post(
        f"/admin/sighting/{sighting_id}/action",
        data={"csrf_token": auth.csrf_for(sid), "action": "hide"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = app_db.execute("SELECT status FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    assert row["status"] == "hidden_by_admin"

    client.post(
        f"/admin/sighting/{sighting_id}/action",
        data={"csrf_token": auth.csrf_for(sid), "action": "unhide"},
        follow_redirects=False,
    )
    row = app_db.execute("SELECT status FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    assert row["status"] == "live"


def test_feature_action(client, app_db):
    sighting_id = seed(app_db)
    sid = _admin(client, app_db)
    client.post(
        f"/admin/sighting/{sighting_id}/action",
        data={"csrf_token": auth.csrf_for(sid), "action": "feature"},
        follow_redirects=False,
    )
    row = app_db.execute("SELECT featured FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    assert row["featured"] == 1


def test_action_rejects_bad_csrf(client, app_db):
    sighting_id = seed(app_db)
    _admin(client, app_db)
    r = client.post(
        f"/admin/sighting/{sighting_id}/action",
        data={"csrf_token": "forged", "action": "hide"},
    )
    assert r.status_code == 403


def test_action_rejects_unknown_action(client, app_db):
    sighting_id = seed(app_db)
    sid = _admin(client, app_db)
    r = client.post(
        f"/admin/sighting/{sighting_id}/action",
        data={"csrf_token": auth.csrf_for(sid), "action": "explode"},
    )
    assert r.status_code == 400


def test_status_page_requires_admin(client, app_db):
    assert client.get("/admin/status").status_code == 404


def test_status_page_renders_for_admin(client, app_db, monkeypatch):
    from app import auth, reddit
    monkeypatch.setattr(reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(reddit, "read_token", lambda: "tok")
    monkeypatch.setattr("app.extract.extract_fields", lambda text: {"date": "2025-07-01"})
    tok = _admin(client, app_db)
    r = client.get("/admin/status")
    assert r.status_code == 200
    assert "Bot login" in r.text and "OK" in r.text
    assert "YouTube queue" in r.text


def test_basic_auth_disabled_without_password(client, app_db):
    # no ADMIN_PASSWORD in the test env -> admin stays a hidden 404
    assert client.get("/admin").status_code == 404


def test_basic_auth_challenge_and_login(client, app_db, monkeypatch):
    import base64
    from app.config import get_settings
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-strong")
    get_settings.cache_clear()
    r = client.get("/admin")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    # wrong password
    bad = base64.b64encode(b"tmosh:wrong").decode()
    assert client.get("/admin", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    # correct credentials -> page + session cookie for site-wide admin state
    good = base64.b64encode(b"tmosh:hunter2-strong").decode()
    r = client.get("/admin", headers={"Authorization": f"Basic {good}"})
    assert r.status_code == 200
    assert "sid" in r.cookies
    # session now carries admin across the site (no Basic header needed)
    r2 = client.get("/admin/status")
    assert r2.status_code == 200


def test_basic_auth_rejects_non_admin_username(client, app_db, monkeypatch):
    import base64
    from app.config import get_settings
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-strong")
    get_settings.cache_clear()
    creds = base64.b64encode(b"randomguy:hunter2-strong").decode()
    assert client.get("/admin", headers={"Authorization": f"Basic {creds}"}).status_code == 401


def test_admin_delete_removes_everything(client, app_db, monkeypatch):
    from app import auth
    from tests.test_public import seed
    deleted_keys = []
    monkeypatch.setattr("app.routes.admin.r2.delete_key", lambda k: deleted_keys.append(k))
    unindexed = []
    monkeypatch.setattr("app.routes.admin.search.delete_sightings",
                        lambda ids: unindexed.extend(ids))
    sid = seed(app_db, title="Delete me sighting")
    app_db.execute("INSERT INTO media (sighting_id, r2_key, kind, thumb_key, display_key) "
                   "VALUES (?, 'uploads/x.jpg', 'image', 'thumbs/x.jpg', 'display/x.jpg')", (sid,))
    app_db.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body) "
                   "VALUES ('dc1', ?, 'a', 'b')", (sid,))
    app_db.commit()
    tok = _admin(client, app_db)
    r = client.post(f"/admin/sighting/{sid}/delete",
                    data={"csrf_token": auth.csrf_for(tok)}, follow_redirects=False)
    assert r.status_code == 303
    assert app_db.execute("SELECT COUNT(*) FROM sightings WHERE id=?", (sid,)).fetchone()[0] == 0
    assert app_db.execute("SELECT COUNT(*) FROM comments WHERE sighting_id=?", (sid,)).fetchone()[0] == 0
    assert sorted(deleted_keys) == ["display/x.jpg", "thumbs/x.jpg", "uploads/x.jpg"]
    assert unindexed == [sid]


def test_admin_delete_needs_csrf(client, app_db):
    from tests.test_public import seed
    sid = seed(app_db)
    _admin(client, app_db)
    r = client.post(f"/admin/sighting/{sid}/delete", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    assert app_db.execute("SELECT COUNT(*) FROM sightings WHERE id=?", (sid,)).fetchone()[0] == 1


def test_review_queue_link_and_ip_collision_flag(client, app_db):
    # two throwaway usernames from one IP, plus an unrelated submission
    a = seed(app_db, title="Queued A", reddit_username="Slight_Perception_95",
             status="pending_review", submitter_ip="203.0.113.7")
    seed(app_db, title="Queued B", reddit_username="Slight_Perception_24",
         status="pending_review", submitter_ip="203.0.113.7")
    seed(app_db, title="Queued C", reddit_username="someone_else",
         status="pending_review", submitter_ip="198.51.100.9")
    _admin(client, app_db)
    r = client.get("/admin/review")
    assert r.status_code == 200
    assert f'href="/sighting/{a}"' in r.text          # full-submission link
    assert "203.0.113.7" in r.text                    # IP surfaced to the mod
    assert "shared with 1 other submission" in r.text  # collision flagged


def test_admin_can_view_pending_review_detail(client, app_db):
    sid = seed(app_db, title="Queued detail", status="pending_review")
    _admin(client, app_db)
    r = client.get(f"/sighting/{sid}")  # 301 -> canonical slug, then 200 for admin
    assert r.status_code == 200
    assert "Queued detail" in r.text


def test_reject_purges_media_but_keeps_audit_stub(client, app_db, monkeypatch):
    from app import auth
    deleted_keys = []
    monkeypatch.setattr("app.routes.admin.r2.delete_key", lambda k: deleted_keys.append(k))
    unindexed = []
    monkeypatch.setattr("app.routes.admin.search.delete_sightings",
                        lambda ids: unindexed.extend(ids))
    sid = seed(app_db, title="Troll spam", status="pending_review",
               reddit_username="throwaway", submitter_ip="203.0.113.9")
    app_db.execute("INSERT INTO media (sighting_id, r2_key, kind, thumb_key, display_key) "
                   "VALUES (?, 'uploads/t.mp4', 'video', 'thumbs/t.jpg', NULL)", (sid,))
    app_db.commit()
    tok = _admin(client, app_db)
    r = client.post(f"/admin/review/{sid}/reject",
                    data={"csrf_token": auth.csrf_for(tok)}, follow_redirects=False)
    assert r.status_code == 303
    # every R2 object for the media is gone
    assert sorted(deleted_keys) == ["thumbs/t.jpg", "uploads/t.mp4"]
    # media rows are gone too, so nothing points at the deleted objects
    assert app_db.execute("SELECT COUNT(*) FROM media WHERE sighting_id=?", (sid,)).fetchone()[0] == 0
    # the sighting row survives as a rejected audit stub (IP/username kept)
    row = app_db.execute("SELECT status, reddit_username, submitter_ip FROM sightings WHERE id=?",
                         (sid,)).fetchone()
    assert row["status"] == "rejected"
    assert row["reddit_username"] == "throwaway" and row["submitter_ip"] == "203.0.113.9"
    assert unindexed == [sid]


def test_reject_needs_csrf(client, app_db):
    from app import auth
    sid = seed(app_db, status="pending_review")
    app_db.execute("INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, 'uploads/z.jpg', 'image')", (sid,))
    app_db.commit()
    _admin(client, app_db)
    r = client.post(f"/admin/review/{sid}/reject", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    # nothing purged on a rejected CSRF
    assert app_db.execute("SELECT COUNT(*) FROM media WHERE sighting_id=?", (sid,)).fetchone()[0] == 1
    assert app_db.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()[0] == "pending_review"


def test_approve_dms_verified_reporter_with_link(client, app_db, monkeypatch):
    from app import auth
    monkeypatch.setattr("app.routes.admin.posting.post_sighting",
                        lambda conn, sid, *, verified: "pid99")
    sent = []
    monkeypatch.setattr("app.notify.reddit.script_token", lambda: "tok")
    monkeypatch.setattr("app.notify.reddit.send_message",
                        lambda tok, *, to, subject, text: sent.append((to, text)))
    sid = seed(app_db, status="pending_review", reddit_username="witness1")
    app_db.execute("UPDATE sightings SET username_verified=1 WHERE id=?", (sid,))
    app_db.commit()
    tok = _admin(client, app_db)
    r = client.post(f"/admin/review/{sid}/approve",
                    data={"csrf_token": auth.csrf_for(tok)}, follow_redirects=False)
    assert r.status_code == 303
    assert sent and sent[0][0] == "witness1" and "reddit.com/comments/pid99" in sent[0][1]


def test_reject_dms_verified_reporter_with_reason(client, app_db, monkeypatch):
    from app import auth
    monkeypatch.setattr("app.routes.admin.r2.delete_key", lambda k: None)
    sent = []
    monkeypatch.setattr("app.notify.reddit.script_token", lambda: "tok")
    monkeypatch.setattr("app.notify.reddit.send_message",
                        lambda tok, *, to, subject, text: sent.append((to, text)))
    sid = seed(app_db, status="pending_review", reddit_username="witness1")
    app_db.execute("UPDATE sightings SET username_verified=1 WHERE id=?", (sid,))
    app_db.commit()
    tok = _admin(client, app_db)
    client.post(f"/admin/review/{sid}/reject",
                data={"csrf_token": auth.csrf_for(tok), "reason": "Does not follow guidelines"},
                follow_redirects=False)
    assert sent and sent[0][0] == "witness1" and "Does not follow guidelines" in sent[0][1]
    # reason is also kept on the rejected audit stub
    assert app_db.execute("SELECT review_reason FROM sightings WHERE id=?", (sid,)).fetchone()[0] \
        == "Does not follow guidelines"


def test_reject_unverified_or_no_reason_sends_no_dm(client, app_db, monkeypatch):
    from app import auth
    monkeypatch.setattr("app.routes.admin.r2.delete_key", lambda k: None)
    sent = []
    monkeypatch.setattr("app.notify.reddit.send_message", lambda *a, **k: sent.append(1))
    # verified but no reason -> no DM
    a = seed(app_db, status="pending_review", reddit_username="a")
    app_db.execute("UPDATE sightings SET username_verified=1 WHERE id=?", (a,))
    # reason given but account unconfirmed -> no DM
    b = seed(app_db, status="pending_review", reddit_username="b")
    app_db.commit()
    tok = _admin(client, app_db)
    client.post(f"/admin/review/{a}/reject", data={"csrf_token": auth.csrf_for(tok)})
    client.post(f"/admin/review/{b}/reject",
                data={"csrf_token": auth.csrf_for(tok), "reason": "spam"})
    assert sent == []


def test_review_lists_prior_reports_from_same_ip(client, app_db):
    seed(app_db, title="Old junk", status="rejected", reddit_username="alt1",
         submitter_ip="203.0.113.50")
    seed(app_db, title="Older live one", status="live", reddit_username="alt2",
         submitter_ip="203.0.113.50")
    seed(app_db, title="New under review", status="pending_review", reddit_username="alt3",
         submitter_ip="203.0.113.50")
    seed(app_db, title="Unrelated", status="pending_review", reddit_username="z",
         submitter_ip="198.51.100.1")
    _admin(client, app_db)
    r = client.get("/admin/review")
    assert r.status_code == 200
    assert "2 earlier report(s) from this IP" in r.text  # rejected + live, across all statuses
    assert "Old junk" in r.text and "Older live one" in r.text
    # the unrelated card has no history line
    assert "earlier report(s) from this IP</strong>\n      (198.51.100.1)" not in r.text


def test_review_shows_verified_vs_not_verified_badges(client, app_db):
    v = seed(app_db, title="Confirmed report", status="pending_review", reddit_username="a")
    app_db.execute("UPDATE sightings SET username_verified=1 WHERE id=?", (v,))
    seed(app_db, title="Lapsed report", status="pending_review", reddit_username="b")
    app_db.commit()
    _admin(client, app_db)
    r = client.get("/admin/review").text
    assert "Verified" in r and "Not verified" in r


def test_awaiting_verification_section_and_no_approve(client, app_db):
    seed(app_db, title="Review me now", status="pending_review", reddit_username="x")
    seed(app_db, title="Not yet confirmed", status="pending_verify", reddit_username="carlos")
    _admin(client, app_db)
    r = client.get("/admin/review")
    assert r.status_code == 200
    assert "Awaiting reporter verification" in r.text
    assert "Not yet confirmed" in r.text and "Review me now" in r.text
    # only the pending_review card is approvable; awaiting items are reject-only
    assert r.text.count("Approve &amp; post") == 1


def test_status_page_shows_awaiting_verify_count(client, app_db, monkeypatch):
    from app import reddit
    monkeypatch.setattr(reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(reddit, "read_token", lambda: "tok")
    monkeypatch.setattr("app.extract.extract_fields", lambda text: {})
    seed(app_db, status="pending_verify")
    _admin(client, app_db)
    assert "Awaiting reporter verification" in client.get("/admin/status").text


def test_per_user_admin_credentials(client, app_db, monkeypatch):
    """A second admin logs in with their OWN password via ADMIN_CREDENTIALS,
    while tmosh's shared ADMIN_PASSWORD still works."""
    import base64
    from app.config import get_settings
    monkeypatch.setenv("ADMIN_PASSWORD", "tmosh-shared-pw")
    monkeypatch.setenv("ADMIN_CREDENTIALS", "AmazonIsDeclining:amz-secret-pw")
    get_settings.cache_clear()

    def _get(user, pw):
        creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return client.get("/admin", headers={"Authorization": f"Basic {creds}"})

    # new admin, own password -> in
    r = _get("AmazonIsDeclining", "amz-secret-pw")
    assert r.status_code == 200 and "sid" in r.cookies
    # new admin, wrong password -> rejected
    client.cookies.clear()
    assert _get("AmazonIsDeclining", "tmosh-shared-pw").status_code == 401
    # tmosh still works on the shared password
    client.cookies.clear()
    assert _get("tmosh", "tmosh-shared-pw").status_code == 200
    get_settings.cache_clear()
