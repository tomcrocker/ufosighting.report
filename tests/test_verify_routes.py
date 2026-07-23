import httpx
import respx

from tests.test_public import seed


def _pending(app_db, token="tok-abc"):
    sid = seed(app_db, status="pending_verify", reddit_username="witness1")
    app_db.execute("UPDATE sightings SET verify_token=? WHERE id=?", (token, sid))
    app_db.commit()
    return sid


def test_valid_token_queues_for_posting(client, app_db, monkeypatch):
    """The click no longer posts inline — it queues, so media processing (a
    video's poster frame especially) can finish before the post goes out."""
    monkeypatch.setattr("app.routes.verify.quality.gate", lambda u: (True, ""))
    sid = _pending(app_db)
    r = client.get("/verify/tok-abc")
    assert r.status_code == 200
    assert "live shortly" in r.text.lower()
    row = app_db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_post"
    assert row["username_verified"] == 1       # identity is confirmed at click time
    assert row["verify_token"] is None         # link is single-use
    assert row["pending_post_at"] is not None  # queue clock starts now
    assert row["reddit_post_id"] is None       # nothing posted yet


def test_unknown_token_friendly(client):
    r = client.get("/verify/nope")
    assert r.status_code == 200 and "no longer valid" in r.text.lower()


def test_used_token_friendly(client, app_db):
    # a live sighting with no token — the link was already consumed
    seed(app_db, status="live", reddit_username="w")
    r = client.get("/verify/anything")
    assert r.status_code == 200 and "no longer valid" in r.text.lower()


# --- deferred posting: the worker drains the queue ---

def _queued(app_db, **over):
    """A sighting sitting in the post queue, as the verify click leaves it."""
    sid = seed(app_db, status="pending_post", reddit_username="witness1", **over)
    app_db.execute(
        "UPDATE sightings SET username_verified=1, "
        "pending_post_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (sid,))
    app_db.commit()
    return sid


def _add_media(app_db, sid, *, kind="video", thumb=None):
    app_db.execute(
        "INSERT INTO media (sighting_id, r2_key, kind, thumb_key, sort_order) VALUES (?,?,?,?,0)",
        (sid, f"uploads/2026/07/{'a'*32}.mp4", kind, thumb))
    app_db.commit()


def test_queue_waits_for_video_poster(app_db, monkeypatch):
    from app import posting
    posted = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: posted.append(sid))
    sid = _queued(app_db)
    _add_media(app_db, sid, kind="video", thumb=None)   # poster not ready
    assert posting.process_post_queue(app_db) == 0
    assert posted == []                                  # held back, not dropped
    app_db.execute("UPDATE media SET thumb_key='thumbs/x.jpg' WHERE sighting_id=?", (sid,))
    app_db.commit()
    assert posting.process_post_queue(app_db) == 1
    assert posted == [sid]                               # posts once the poster exists


def test_queue_posts_immediately_without_media(app_db, monkeypatch):
    from app import posting
    posted = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: posted.append((sid, verified)))
    sid = _queued(app_db)
    assert posting.process_post_queue(app_db) == 1
    assert posted == [(sid, True)]  # verified click carries through to the post tag


def test_queue_timeout_posts_anyway(app_db, monkeypatch):
    """A thumbnail that never finishes must delay a post, never lose it."""
    from app import posting
    posted = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: posted.append(sid))
    sid = _queued(app_db)
    _add_media(app_db, sid, kind="video", thumb=None)
    assert posting.process_post_queue(app_db) == 0
    app_db.execute(
        "UPDATE sightings SET pending_post_at="
        "strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes') WHERE id=?", (sid,))
    app_db.commit()
    assert posting.process_post_queue(app_db) == 1
    assert posted == [sid]


def test_queue_gives_up_after_max_attempts(app_db, monkeypatch):
    from app import posting

    def boom(conn, sid, *, verified):
        raise RuntimeError("reddit down")

    monkeypatch.setattr(posting, "post_sighting", boom)
    sid = _queued(app_db)
    for _ in range(posting.MAX_POST_ATTEMPTS):
        posting.process_post_queue(app_db)
    row = app_db.execute("SELECT status, post_attempts FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["post_attempts"] == posting.MAX_POST_ATTEMPTS
    assert row["status"] == "pending_post"      # kept for a human, not silently dropped
    assert posting.process_post_queue(app_db) == 0  # stops hammering Reddit


def test_queue_ignores_rows_predating_this_flow(app_db, monkeypatch):
    """A pending_post row with no queue timestamp predates deferred posting.
    Auto-posting those on deploy would blast stale sightings to r/UFOs."""
    from app import posting
    posted = []
    monkeypatch.setattr(posting, "post_sighting",
                        lambda conn, sid, *, verified: posted.append(sid))
    sid = seed(app_db, status="pending_post", reddit_username="old")
    app_db.execute("UPDATE sightings SET pending_post_at=NULL WHERE id=?", (sid,))
    app_db.commit()
    assert posting.process_post_queue(app_db) == 0
    assert posted == []


# --- CQS-proxy gate at verify time ---

def test_good_account_auto_queues(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.verify.quality.gate", lambda u: (True, ""))
    sid = _pending(app_db)
    r = client.get("/verify/tok-abc")
    assert "live shortly" in r.text.lower()
    row = app_db.execute("SELECT status, review_reason FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_post" and row["review_reason"] is None


def test_low_cqs_account_routed_to_review(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.verify.quality.gate",
                        lambda u: (False, "new account (2 days old)"))
    sid = _pending(app_db)
    r = client.get("/verify/tok-abc")
    # honest, non-insulting message — never tells the reporter they're "low quality"
    assert "review" in r.text.lower() and "low" not in r.text.lower()
    row = app_db.execute("SELECT status, review_reason, username_verified FROM sightings WHERE id=?",
                         (sid,)).fetchone()
    assert row["status"] == "pending_review"
    assert row["review_reason"] == "new account (2 days old)"
    assert row["username_verified"] == 1  # identity still confirmed


def test_banned_reporter_never_reaches_post_queue(client, app_db, monkeypatch):
    monkeypatch.setattr("app.routes.verify.quality.gate",
                        lambda u: (False, "🚫 BANNED on r/UFOs — do not approve (ban evasion)"))
    sid = _pending(app_db)
    client.get("/verify/tok-abc")
    row = app_db.execute("SELECT status, review_reason FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_review" and "BANNED" in row["review_reason"]


def test_global_hold_skips_the_gate(client, app_db, monkeypatch):
    from app import appsettings
    called = []
    monkeypatch.setattr("app.routes.verify.quality.gate",
                        lambda u: called.append(u) or (True, ""))
    appsettings.set(app_db, appsettings.HOLD_POSTS, "1")
    sid = _pending(app_db)
    client.get("/verify/tok-abc")
    row = app_db.execute("SELECT status, review_reason FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_review"
    assert "moderation hold" in row["review_reason"]
    assert called == []  # hold short-circuits before any Reddit API call
