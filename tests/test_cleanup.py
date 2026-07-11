from datetime import datetime, timedelta, timezone

import cleanup
from app import auth
from tests.test_db import _insert_sighting

OLD = datetime.now(timezone.utc) - timedelta(hours=72)
FRESH = datetime.now(timezone.utc) - timedelta(hours=1)

REFERENCED = "uploads/2026/07/" + "1" * 32 + ".jpg"
ORPHAN_OLD = "uploads/2026/07/" + "2" * 32 + ".jpg"
ORPHAN_FRESH = "uploads/2026/07/" + "3" * 32 + ".jpg"


def test_cleanup_uploads_deletes_only_old_orphans(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, REFERENCED),
    )
    db_conn.commit()

    deleted = []
    monkeypatch.setattr(
        cleanup.r2, "list_keys",
        lambda prefix: iter([(REFERENCED, OLD), (ORPHAN_OLD, OLD), (ORPHAN_FRESH, FRESH)]),
    )
    monkeypatch.setattr(cleanup.r2, "delete_key", deleted.append)

    assert cleanup.cleanup_uploads(db_conn, older_than_hours=48) == 1
    assert deleted == [ORPHAN_OLD]


def test_cleanup_sessions_removes_expired(db_conn):
    auth.create_session(db_conn, "fresh", "tok", ttl_seconds=3600)
    auth.create_session(db_conn, "stale", "tok", ttl_seconds=-100)
    assert cleanup.cleanup_sessions(db_conn) == 1
    remaining = db_conn.execute("SELECT username FROM sessions").fetchall()
    assert [r["username"] for r in remaining] == ["fresh"]


def test_cleanup_drafts_removes_old(db_conn):
    auth.save_draft(db_conn, "recent", {"title": "x"})
    db_conn.execute(
        """INSERT INTO drafts (username, form_json, updated_at)
           VALUES ('ancient', '{}', strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-10 days'))"""
    )
    db_conn.commit()
    assert cleanup.cleanup_drafts(db_conn, older_than_days=7) == 1
    remaining = db_conn.execute("SELECT username FROM drafts").fetchall()
    assert [r["username"] for r in remaining] == ["recent"]


def test_cleanup_pending_removes_stale_rows_and_media(db_conn):
    sid = _insert_sighting(db_conn)  # default status is pending_post
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, REFERENCED),
    )
    db_conn.execute(
        "UPDATE sightings SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ','now','-3 hours') WHERE id=?",
        (sid,),
    )
    live = _insert_sighting(db_conn)
    db_conn.execute("UPDATE sightings SET status='live' WHERE id=?", (live,))
    db_conn.commit()

    assert cleanup.cleanup_pending(db_conn, older_than_hours=1) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_cleanup_rate_events(db_conn):
    import cleanup
    db_conn.execute("INSERT INTO rate_events (ip, action, created_at) "
                    "VALUES ('1','submit', strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 days'))")
    db_conn.execute("INSERT INTO rate_events (ip, action) VALUES ('1','submit')")
    db_conn.commit()
    assert cleanup.cleanup_rate_events(db_conn) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0] == 1
