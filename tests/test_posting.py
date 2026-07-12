import httpx
import respx

from app import posting
from tests.test_db import _insert_sighting


def _seed_ready(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("UPDATE sightings SET status='pending_verify', reddit_username='witness1', "
                    "verify_token='tok123' WHERE id=?", (sid,))
    db_conn.commit()
    return sid


@respx.mock
def test_post_sighting_verified(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_zzz"}}}))
    sid = _seed_ready(db_conn)
    pid = posting.post_sighting(db_conn, sid, verified=True)
    assert pid == "zzz"
    row = db_conn.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live" and row["reddit_post_id"] == "zzz"
    assert row["username_verified"] == 1 and row["verify_token"] is None


@respx.mock
def test_post_sighting_self_reported_attribution(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    route = respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_qq"}}}))
    sid = _seed_ready(db_conn)
    posting.post_sighting(db_conn, sid, verified=False)
    body = route.calls[0].request.content
    assert b"self-reported" in body


def _mk_media(conn, sid, *rows):
    for i, (key, kind, thumb) in enumerate(rows):
        conn.execute("INSERT INTO media (sighting_id, r2_key, kind, thumb_key, sort_order)"
                     " VALUES (?,?,?,?,?)", (sid, key, kind, thumb, i))
    conn.commit()


def _native_stubs(monkeypatch, calls):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting, "_fetch_r2", lambda key: b"bytes-" + key.encode())
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id",
                        lambda *a, **k: None)
    monkeypatch.setattr(posting.reddit_media, "upload_asset",
                        lambda tok, fn, mt, data: calls.setdefault("uploads", []).append((fn, mt))
                        or posting.reddit_media.Asset(f"as{len(calls['uploads'])}", f"https://u/{fn}"))
    monkeypatch.setattr(posting.reddit_media, "comment",
                        lambda tok, *, post_id, text: calls.update(comment=(post_id, text)))


def test_video_first_native_post(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None),
              ("uploads/b.mp4", "video", "thumbs/b.jpg"))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_video",
                        lambda tok, **k: calls.update(video=k))
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id",
                        lambda tok, **k: "vid001")
    pid = posting.post_sighting(db_conn, sid, verified=True)
    assert pid == "vid001"
    assert "video_url" in calls["video"] and "poster_url" in calls["video"]
    assert calls["comment"][0] == "vid001"
    assert "ufosighting.report" in calls["comment"][1]
    row = db_conn.execute("SELECT status, reddit_post_id FROM sightings WHERE id=?",
                          (sid,)).fetchone()
    assert row["status"] == "live" and row["reddit_post_id"] == "vid001"


def test_single_image_native(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image",
                        lambda tok, **k: calls.update(image=k))
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id",
                        lambda tok, **k: "img001")
    assert posting.post_sighting(db_conn, sid, verified=False) == "img001"
    assert calls["image"]["image_url"] == "https://u/a.jpg"


def test_multi_image_gallery(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None),
              ("uploads/b.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_gallery",
                        lambda tok, **k: calls.update(gallery=k) or "gal001")
    assert posting.post_sighting(db_conn, sid, verified=True) == "gal001"
    assert calls["gallery"]["asset_ids"] == ["as1", "as2"]


def test_upload_failure_falls_back_to_self_post(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id", lambda *a, **k: None)
    monkeypatch.setattr(posting, "_fetch_r2", lambda key: b"x")

    def boom(*a, **k):
        raise posting.reddit.RedditError("lease failed")

    monkeypatch.setattr(posting.reddit_media, "upload_asset", boom)
    body_seen = {}

    def fake_self(tok, **k):
        body_seen.update(k)
        return "self001"

    monkeypatch.setattr(posting.reddit, "submit_post", fake_self)
    assert posting.post_sighting(db_conn, sid, verified=True) == "self001"
    # fallback self post keeps the media URLs in the body
    assert "media.test" in body_seen["body"]


def test_poll_timeout_raises_no_fallback(db_conn, monkeypatch):
    import pytest
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: None)
    fell_back = []
    monkeypatch.setattr(posting.reddit, "submit_post",
                        lambda tok, **k: fell_back.append(1) or "self001")
    with pytest.raises(posting.reddit.RedditError):
        posting.post_sighting(db_conn, sid, verified=True)
    assert not fell_back
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending_verify"


def test_retry_adopts_existing_post(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id",
                        lambda *a, **k: "adopted1")
    monkeypatch.setattr(posting.reddit_media, "comment",
                        lambda tok, *, post_id, text: calls.update(comment=post_id))
    submitted = []
    monkeypatch.setattr(posting.reddit_media, "upload_asset",
                        lambda *a, **k: submitted.append(1))
    assert posting.post_sighting(db_conn, sid, verified=True) == "adopted1"
    assert not submitted and calls["comment"] == "adopted1"


def test_comment_failure_nonfatal(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img9")

    def bad_comment(tok, *, post_id, text):
        raise posting.reddit.RedditError("comment blocked")

    monkeypatch.setattr(posting.reddit_media, "comment", bad_comment)
    assert posting.post_sighting(db_conn, sid, verified=True) == "img9"
    row = db_conn.execute("SELECT status FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "live"


def test_video_without_thumb_falls_back_to_self(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/b.mp4", "video", None))
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id", lambda *a, **k: None)
    monkeypatch.setattr(posting.reddit, "submit_post", lambda tok, **k: "self42")
    assert posting.post_sighting(db_conn, sid, verified=True) == "self42"


def test_spam_removed_native_post_self_approves(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img77")
    monkeypatch.setattr(posting.reddit, "fetch_post",
                        lambda tok, pid: {"id": pid, "removed_by_category": "reddit"})
    monkeypatch.setattr(posting.reddit, "approve",
                        lambda tok, *, post_id: calls.update(approved=post_id))
    assert posting.post_sighting(db_conn, sid, verified=True) == "img77"
    assert calls["approved"] == "img77"


def test_live_native_post_not_approved(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img88")
    monkeypatch.setattr(posting.reddit, "fetch_post",
                        lambda tok, pid: {"id": pid, "removed_by_category": None})
    approved = []
    monkeypatch.setattr(posting.reddit, "approve",
                        lambda tok, *, post_id: approved.append(post_id))
    posting.post_sighting(db_conn, sid, verified=True)
    assert not approved
