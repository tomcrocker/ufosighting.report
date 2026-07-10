def _insert_sighting(conn, **over):
    row = {
        "reddit_username": "tester", "title": "Bright orb over the lake",
        "description": "A silent orange light hovered for two minutes.",
        "sighted_at": "2026-07-01T05:00:00Z", "location_text": "Lake Cowichan, BC",
    }
    row.update(over)
    cur = conn.execute(
        """INSERT INTO sightings (reddit_username, title, description, sighted_at, location_text)
           VALUES (:reddit_username, :title, :description, :sighted_at, :location_text)""",
        row,
    )
    conn.commit()
    return cur.lastrowid


def test_init_db_is_idempotent(db_conn):
    from app import db
    db.init_db(db_conn)  # second run must not raise


def test_wal_and_foreign_keys_enabled(db_conn):
    assert db_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_defaults(db_conn):
    sid = _insert_sighting(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_post"
    assert row["source"] == "site"
    assert row["featured"] == 0
    assert row["reddit_score"] == 0
    assert row["location_obscured"] == 0


def test_fts_insert_update_delete_stay_in_sync(db_conn):
    sid = _insert_sighting(db_conn)
    match = lambda q: db_conn.execute(
        "SELECT rowid FROM sightings_fts WHERE sightings_fts MATCH ?", (q,)
    ).fetchall()
    assert len(match('"orb"')) == 1
    db_conn.execute("UPDATE sightings SET title='Black triangle craft' WHERE id=?", (sid,))
    db_conn.commit()
    assert len(match('"orb"')) == 0
    assert len(match('"triangle"')) == 1
    db_conn.execute("DELETE FROM sightings WHERE id=?", (sid,))
    db_conn.commit()
    assert len(match('"triangle"')) == 0


def test_media_cascade_delete(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, "uploads/2026/07/aabbccddeeff00112233445566778899.jpg"),
    )
    db_conn.commit()
    db_conn.execute("DELETE FROM sightings WHERE id=?", (sid,))
    db_conn.commit()
    n = db_conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    assert n == 0
