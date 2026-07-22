from app import orphans
from tests.test_db import _insert_sighting


def _key(n="a"):
    return f"uploads/2026/07/{n * 32}.mp4"


def test_attached_upload_is_not_an_orphan(db_conn):
    sid = _insert_sighting(db_conn)
    k = _key("a")
    orphans.record_key(db_conn, key=k, ip="1.2.3.4", kind="video")
    db_conn.execute("INSERT INTO media (sighting_id, r2_key, kind, sort_order) VALUES (?,?,?,0)",
                    (sid, k, "video"))
    db_conn.execute("UPDATE upload_keys SET created_at="
                    "strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
    db_conn.commit()
    assert orphans.count(db_conn) == 0


def test_unattached_upload_is_reported_after_grace_period(db_conn):
    """The 127MB video lost on sighting 12618 looked exactly like this."""
    orphans.record_key(db_conn, key=_key("b"), ip="1.2.3.4", kind="video")
    db_conn.commit()
    assert orphans.count(db_conn) == 0  # still inside the grace period
    db_conn.execute("UPDATE upload_keys SET created_at="
                    "strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
    db_conn.commit()
    assert orphans.count(db_conn) == 1
    row = orphans.find(db_conn)[0]
    assert row["key"] == _key("b") and row["kind"] == "video"


def test_record_key_is_idempotent(db_conn):
    for _ in range(3):
        orphans.record_key(db_conn, key=_key("c"), ip="1.2.3.4", kind="image")
    assert db_conn.execute("SELECT COUNT(*) FROM upload_keys").fetchone()[0] == 1


def test_warn_for_submission_flags_only_the_dropped_file(db_conn, capsys):
    attached, dropped = _key("d"), _key("e")
    for k in (attached, dropped):
        orphans.record_key(db_conn, key=k, ip="9.9.9.9", kind="video")
    orphans.record_key(db_conn, key=_key("f"), ip="8.8.8.8", kind="video")  # someone else
    missing = orphans.warn_for_submission(
        db_conn, ip="9.9.9.9", attached=[attached], sighting_id=12618)
    assert missing == [dropped]
    assert "sighting 12618 left 1 uploaded" in capsys.readouterr().out


def test_warn_for_submission_silent_when_all_attached(db_conn, capsys):
    k = _key("g")
    orphans.record_key(db_conn, key=k, ip="9.9.9.9", kind="image")
    assert orphans.warn_for_submission(
        db_conn, ip="9.9.9.9", attached=[k], sighting_id=1) == []
    assert capsys.readouterr().out == ""
