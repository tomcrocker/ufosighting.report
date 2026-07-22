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
    respx.get("https://oauth.reddit.com/api/info").mock(  # rescue check: post is live
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "zzz", "removed_by_category": None}}]}}))
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
    respx.get("https://oauth.reddit.com/api/info").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "qq", "removed_by_category": None}}]}}))
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


def test_native_post_captures_reddit_posted_at(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img55")
    # fetch_post (also used by the spam rescue) carries created_utc
    monkeypatch.setattr(posting.reddit, "fetch_post",
                        lambda tok, pid: {"removed_by_category": None,
                                          "created_utc": 1784139840})
    monkeypatch.setattr(posting.reddit, "approve", lambda tok, **k: None)
    posting.post_sighting(db_conn, sid, verified=True)
    row = db_conn.execute("SELECT reddit_posted_at FROM sightings WHERE id=?",
                          (sid,)).fetchone()
    assert row["reddit_posted_at"] == "2026-07-15T18:24:00Z"


def test_details_comment_gets_pinned(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img99")
    monkeypatch.setattr(posting.reddit_media, "comment",
                        lambda tok, *, post_id, text: "cmt99")
    monkeypatch.setattr(posting.reddit_media, "pin_comment",
                        lambda tok, *, comment_id: calls.update(pinned=comment_id))
    monkeypatch.setattr(posting.reddit, "fetch_post",
                        lambda tok, pid: {"removed_by_category": None})
    monkeypatch.setattr(posting.reddit, "approve",
                        lambda tok, **k: calls.update(approved=k))
    posting.post_sighting(db_conn, sid, verified=True)
    assert calls["pinned"] == "cmt99"
    assert calls["approved"] == {"comment_id": "cmt99"}  # preemptive approve


def test_heic_uploads_display_derivative_to_reddit(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    db_conn.execute("INSERT INTO media (sighting_id, r2_key, kind, thumb_key, display_key, sort_order)"
                    " VALUES (?,?,?,?,?,0)",
                    (sid, "uploads/2026/07/abc.heic", "image", "thumbs/abc.jpg",
                     "display/2026/07/abc.jpg"))
    db_conn.commit()
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image",
                        lambda tok, **k: calls.update(image=k))
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "h1")
    monkeypatch.setattr(posting.reddit, "fetch_post",
                        lambda tok, pid: {"removed_by_category": None})
    monkeypatch.setattr(posting.reddit, "approve", lambda tok, **k: None)
    posting.post_sighting(db_conn, sid, verified=True)
    # the JPEG derivative went to Reddit, not the HEIC original
    assert calls["uploads"] == [("abc.jpg", "image/jpeg")]


def test_fallback_self_post_also_self_approves(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)  # no media -> self post path
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "find_recent_post_id", lambda *a, **k: None)
    monkeypatch.setattr(posting.reddit, "submit_post", lambda tok, **k: "self5")
    monkeypatch.setattr(posting.reddit, "fetch_post",
                        lambda tok, pid: {"removed_by_category": "reddit"})
    approved = []
    monkeypatch.setattr(posting.reddit, "approve",
                        lambda tok, **k: approved.append(k.get("post_id")))
    assert posting.post_sighting(db_conn, sid, verified=True) == "self5"
    assert approved == ["self5"]


# --- sky context in the pinned comment ---

def _geocode(conn, sid):
    conn.execute("UPDATE sightings SET lat=48.8123, lon=-124.1456 WHERE id=?", (sid,))
    conn.commit()


def _submitted_text(route):
    """The post body, decoded out of the urlencoded submit form."""
    from urllib.parse import parse_qs
    return parse_qs(route.calls[0].request.content.decode())["text"][0]


@respx.mock
def test_post_body_carries_sky_links_when_geocoded(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    route = respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_sk"}}}))
    respx.get("https://oauth.reddit.com/api/info").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "sk", "removed_by_category": None}}]}}))
    sid = _seed_ready(db_conn)
    _geocode(db_conn, sid)
    posting.post_sighting(db_conn, sid, verified=True)
    body = _submitted_text(route)
    assert "Sky context for this time and place" in body
    assert "globe.adsbexchange.com" in body and "heavens-above.com" in body
    # sky_events is still NULL at post time, so no computed claims yet
    assert "No bright satellites" not in body


@respx.mock
def test_post_body_omits_sky_block_without_coords(db_conn, monkeypatch):
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "bot-tok")
    route = respx.post("https://oauth.reddit.com/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_ng"}}}))
    respx.get("https://oauth.reddit.com/api/info").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "ng", "removed_by_category": None}}]}}))
    sid = _seed_ready(db_conn)  # no lat/lon
    posting.post_sighting(db_conn, sid, verified=True)
    assert "Sky context" not in _submitted_text(route)


def test_native_post_stores_bot_comment_id(db_conn, monkeypatch):
    sid = _seed_ready(db_conn)
    _mk_media(db_conn, sid, ("uploads/a.jpg", "image", None))
    calls = {}
    _native_stubs(monkeypatch, calls)
    monkeypatch.setattr(posting.reddit_media, "submit_image", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit_media, "wait_for_post_id", lambda tok, **k: "img77")
    monkeypatch.setattr(posting.reddit_media, "comment", lambda tok, *, post_id, text: "cmt77")
    monkeypatch.setattr(posting.reddit_media, "pin_comment", lambda tok, **k: None)
    monkeypatch.setattr(posting.reddit, "fetch_post", lambda tok, pid: {"removed_by_category": None})
    monkeypatch.setattr(posting.reddit, "approve", lambda tok, **k: None)
    posting.post_sighting(db_conn, sid, verified=True)
    row = db_conn.execute("SELECT bot_comment_id FROM sightings WHERE id=?", (sid,)).fetchone()
    assert row["bot_comment_id"] == "cmt77"  # needed so the sky worker can edit it


def test_refresh_sky_comment_edits_in_computed_passes(db_conn, monkeypatch):
    import json as _json
    sid = _seed_ready(db_conn)
    _geocode(db_conn, sid)
    sats = {"checked": True, "catalog_date": "2026-07-01", "bright": [], "launches": [],
            "iss": None, "starlink_visible": 23,
            "trains": [{"count": 23, "az": "NW", "time": "05:28"}]}
    db_conn.execute("UPDATE sightings SET bot_comment_id='cmtX', sky_events=? WHERE id=?",
                    (_json.dumps(sats), sid))
    db_conn.commit()
    edited = {}
    monkeypatch.setattr(posting.reddit, "script_token", lambda: "tok")
    monkeypatch.setattr(posting.reddit_media, "edit_comment",
                        lambda tok, *, comment_id, text: edited.update(id=comment_id, text=text))
    assert posting.refresh_sky_comment(db_conn, sid) is True
    assert edited["id"] == "cmtX"
    assert "Starlink train overhead" in edited["text"] and "23 satellites" in edited["text"]
    assert "globe.adsbexchange.com" in edited["text"]  # links survive the edit


def test_refresh_sky_comment_noop_without_comment_or_data(db_conn, monkeypatch):
    import json as _json
    monkeypatch.setattr(posting.reddit_media, "edit_comment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not edit")))
    sid = _seed_ready(db_conn)
    assert posting.refresh_sky_comment(db_conn, sid) is False   # no comment, no data
    db_conn.execute("UPDATE sightings SET bot_comment_id='c1' WHERE id=?", (sid,))
    db_conn.commit()
    assert posting.refresh_sky_comment(db_conn, sid) is False   # comment but no sky data
    db_conn.execute("UPDATE sightings SET sky_events=? WHERE id=?",
                    (_json.dumps({"checked": False, "reason": "no TLEs"}), sid))
    db_conn.commit()
    assert posting.refresh_sky_comment(db_conn, sid) is False   # unchecked -> no claims
