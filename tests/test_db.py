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


def test_new_columns_exist(db_conn):
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(sightings)")}
    assert {"submitter_ip", "username_verified", "verify_token", "verify_sent_at"} <= cols


def test_verify_defaults(db_conn):
    sid = _insert_sighting(db_conn)
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["username_verified"] == 0
    assert row["verify_token"] is None


def test_rate_events_table(db_conn):
    db_conn.execute("INSERT INTO rate_events (ip, action) VALUES ('1.2.3.4','submit')")
    db_conn.commit()
    n = db_conn.execute("SELECT COUNT(*) FROM rate_events WHERE ip='1.2.3.4'").fetchone()[0]
    assert n == 1


def test_migration_adds_columns_to_legacy_table(tmp_path):
    """Simulates the real prod table (full pre-pivot schema, missing only the 4
    new columns) and confirms init_db ALTERs them in without crashing."""
    import sqlite3
    from app import db
    p = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(p)
    # pre-pivot sightings schema: everything EXCEPT the 4 migration columns
    raw.execute("""CREATE TABLE sightings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT DEFAULT 'site',
        reddit_username TEXT, title TEXT, description TEXT, sighted_at TEXT,
        tz_name TEXT DEFAULT 'UTC', shape TEXT, location_text TEXT, country TEXT,
        location_obscured INTEGER DEFAULT 0, reddit_post_id TEXT UNIQUE,
        status TEXT DEFAULT 'pending_post', featured INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))""")
    raw.execute("INSERT INTO sightings (reddit_username, title, sighted_at) "
                "VALUES ('legacy', 'Old sighting', '2026-01-01T00:00:00Z')")
    raw.commit(); raw.close()
    conn = db.connect(p)
    db.init_db(conn)  # must ALTER the 4 new columns, build indexes, not crash
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    assert {"verify_token", "submitter_ip", "username_verified", "verify_sent_at"} <= cols
    # existing row preserved and new column defaulted
    row = conn.execute("SELECT * FROM sightings WHERE reddit_username='legacy'").fetchone()
    assert row["username_verified"] == 0
    conn.close()


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
