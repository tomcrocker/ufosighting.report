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
