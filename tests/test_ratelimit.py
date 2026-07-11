from app import ratelimit


def test_allowed_until_limit(db_conn):
    for _ in range(3):
        assert ratelimit.allowed(db_conn, "1.1.1.1", "submit", limit=3)
        ratelimit.record(db_conn, "1.1.1.1", "submit")
    assert not ratelimit.allowed(db_conn, "1.1.1.1", "submit", limit=3)


def test_separate_ips_and_actions(db_conn):
    ratelimit.record(db_conn, "1.1.1.1", "submit")
    assert ratelimit.count_recent(db_conn, "2.2.2.2", "submit", 1) == 0
    assert ratelimit.count_recent(db_conn, "1.1.1.1", "presign", 1) == 0
    assert ratelimit.count_recent(db_conn, "1.1.1.1", "submit", 1) == 1


def test_window_excludes_old(db_conn):
    db_conn.execute(
        "INSERT INTO rate_events (ip, action, created_at) "
        "VALUES ('1.1.1.1','submit', strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours'))"
    )
    db_conn.commit()
    assert ratelimit.count_recent(db_conn, "1.1.1.1", "submit", 1) == 0
