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
