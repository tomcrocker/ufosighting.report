from app import verify
from tests.test_db import _insert_sighting


def test_new_token_unguessable():
    a, b = verify.new_token(), verify.new_token()
    assert a != b and len(a) >= 32


def test_verify_message_contains_url():
    subject, text = verify.verify_message("witness1", "https://ufosighting.report/verify/abc")
    assert "ufosighting.report/verify/abc" in text
    assert "ignore" in text.lower()


def test_sweep_moves_stale_pending_verify(db_conn):
    old = _insert_sighting(db_conn)
    db_conn.execute(
        "UPDATE sightings SET status='pending_verify', "
        "verify_sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 hours') WHERE id=?", (old,))
    fresh = _insert_sighting(db_conn)
    db_conn.execute(
        "UPDATE sightings SET status='pending_verify', "
        "verify_sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hours') WHERE id=?", (fresh,))
    db_conn.commit()
    moved = verify.sweep_pending_verify(db_conn, window_hours=6)
    assert moved == 1
    assert db_conn.execute("SELECT status FROM sightings WHERE id=?", (old,)).fetchone()[0] == "pending_review"
    assert db_conn.execute("SELECT status FROM sightings WHERE id=?", (fresh,)).fetchone()[0] == "pending_verify"
