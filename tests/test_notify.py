from app import notify
from tests.test_db import _insert_sighting


def _verified(conn):
    sid = _insert_sighting(conn, reddit_username="witness1")
    conn.execute("UPDATE sightings SET username_verified=1 WHERE id=?", (sid,))
    conn.commit()
    return sid


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(notify.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(notify.reddit, "send_message",
                        lambda tok, *, to, subject, text: sent.append((to, subject, text)))
    return sent


def test_approval_dm_links_the_reddit_post(db_conn, monkeypatch):
    sent = _capture(monkeypatch)
    assert notify.approval_dm(db_conn, _verified(db_conn), "abc123") is True
    to, subject, text = sent[0]
    assert to == "witness1" and "live" in subject.lower()
    assert "reddit.com/comments/abc123" in text


def test_approval_dm_skips_unverified_account(db_conn, monkeypatch):
    sent = _capture(monkeypatch)
    sid = _insert_sighting(db_conn, reddit_username="stranger")  # not verified
    assert notify.approval_dm(db_conn, sid, "abc") is False and sent == []


def test_approval_dm_needs_a_post_id(db_conn, monkeypatch):
    sent = _capture(monkeypatch)
    assert notify.approval_dm(db_conn, _verified(db_conn), None) is False and sent == []


def test_rejection_dm_carries_the_reason(db_conn, monkeypatch):
    sent = _capture(monkeypatch)
    assert notify.rejection_dm(db_conn, _verified(db_conn), "Does not follow guidelines") is True
    assert "Does not follow guidelines" in sent[0][2]


def test_rejection_dm_silent_without_a_reason(db_conn, monkeypatch):
    sent = _capture(monkeypatch)
    assert notify.rejection_dm(db_conn, _verified(db_conn), "   ") is False and sent == []


def test_rejection_dm_skips_unverified_account(db_conn, monkeypatch):
    sent = _capture(monkeypatch)
    sid = _insert_sighting(db_conn, reddit_username="stranger")
    assert notify.rejection_dm(db_conn, sid, "spam") is False and sent == []


def test_dm_failure_is_swallowed(db_conn, monkeypatch):
    monkeypatch.setattr(notify.reddit, "script_token", lambda: "tok")
    def boom(*a, **k):
        raise notify.reddit.RedditError("RATELIMIT")
    monkeypatch.setattr(notify.reddit, "send_message", boom)
    assert notify.approval_dm(db_conn, _verified(db_conn), "x") is False  # no exception
