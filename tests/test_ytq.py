import ytq


def _mk_job(conn, url="https://www.youtube.com/watch?v=XHWPQEJ_TVA"):
    conn.execute("INSERT INTO sightings (reddit_username, title, sighted_at, status) "
                 "VALUES ('u','t','2026-01-01T00:00:00Z','live')")
    sid = conn.execute("SELECT MAX(id) FROM sightings").fetchone()[0]
    conn.execute("INSERT INTO yt_jobs (sighting_id, url) VALUES (?,?)", (sid, url))
    return sid, conn.execute("SELECT MAX(id) FROM yt_jobs").fetchone()[0]


def test_claim_lists_pending_only(db_conn):
    sid, jid = _mk_job(db_conn)
    assert ytq.claim(db_conn) == [{"job_id": jid, "sighting_id": sid,
                                   "url": "https://www.youtube.com/watch?v=XHWPQEJ_TVA"}]
    ytq.done(db_conn, jid, "uploads/2026/07/yt_x.mp4", 123)
    assert ytq.claim(db_conn) == []


def test_claim_respects_limit(db_conn):
    for _ in range(3):
        _mk_job(db_conn)
    assert len(ytq.claim(db_conn, limit=2)) == 2


def test_done_inserts_media_and_marks(db_conn):
    sid, jid = _mk_job(db_conn)
    ytq.done(db_conn, jid, "uploads/2026/07/yt_x.mp4", 4567)
    m = db_conn.execute("SELECT * FROM media").fetchone()
    assert (m["sighting_id"], m["kind"], m["r2_key"], m["size_bytes"]) == \
        (sid, "video", "uploads/2026/07/yt_x.mp4", 4567)
    assert db_conn.execute("SELECT status FROM yt_jobs WHERE id=?",
                           (jid,)).fetchone()[0] == "done"


def test_done_unknown_job_exits(db_conn):
    import pytest
    with pytest.raises(SystemExit):
        ytq.done(db_conn, 999, "k", 1)


def test_fail_retries_then_fails(db_conn):
    sid, jid = _mk_job(db_conn)
    ytq.fail(db_conn, jid, "boom")
    row = db_conn.execute("SELECT * FROM yt_jobs WHERE id=?", (jid,)).fetchone()
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "boom")
    ytq.fail(db_conn, jid, "boom2")
    ytq.fail(db_conn, jid, "boom3")
    row = db_conn.execute("SELECT status, attempts FROM yt_jobs WHERE id=?",
                          (jid,)).fetchone()
    assert (row["status"], row["attempts"]) == ("failed", 3)
    assert ytq.claim(db_conn) == []


def test_fail_truncates_error(db_conn):
    sid, jid = _mk_job(db_conn)
    ytq.fail(db_conn, jid, "x" * 1000)
    err = db_conn.execute("SELECT last_error FROM yt_jobs WHERE id=?", (jid,)).fetchone()[0]
    assert len(err) == 300
