import httpx
import respx

from app import comments
from tests.test_db import _insert_sighting


def _listing(children):
    return httpx.Response(200, json=[
        {"data": {"children": []}},               # [0] = the post
        {"data": {"children": children}},          # [1] = comments
    ])


def _c(cid, author, body, score, **over):
    d = {"id": cid, "author": author, "body": body, "score": score,
         "created_utc": 1751000000, "permalink": f"/r/UFOs/comments/p1/x/{cid}/"}
    d.update(over)
    return {"kind": "t1", "data": d}


def test_is_skipped_author():
    for name in ("CollapseBot", "collapsebot", "COLLAPSEBOT", " ufomodbot ",
                 "AutoModerator", "automoderator"):
        assert comments.is_skipped_author(name), name
    for name in ("real_user", "", None, "collapse", "ufomod"):
        assert not comments.is_skipped_author(name), name


@respx.mock
def test_fetch_skips_bot_automod_and_deleted():
    kids = [_c(f"c{i}", f"user{i}", f"body {i}", 100 - i) for i in range(12)]
    kids.insert(0, _c("cb", "modbot", "details comment", 999))  # the bot (conftest SCRIPT_USERNAME)
    kids.insert(0, _c("ca", "AutoModerator", "sticky", 998))
    kids.insert(0, _c("cc", "CollapseBot", "collapsed reply", 996))  # r/UFOs bot
    kids.insert(0, _c("cu", "ufomodbot", "mod note", 995))           # r/UFOs bot
    kids.insert(0, _c("cd", "ghost", "[deleted]", 997))
    kids.append({"kind": "more", "data": {"children": ["x"]}})
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=_listing(kids))
    out = comments.fetch_top_comments("tok", "p1")
    assert len(out) == 12  # filtering at fetch; capping happens in refresh
    authors = {c["author"] for c in out}
    assert not authors & {"AutoModerator", "modbot", "CollapseBot", "ufomodbot"}
    assert all(c["body"] not in ("[deleted]", "[removed]") for c in out)


@respx.mock
def test_fetch_http_error_returns_empty():
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=httpx.Response(500))
    assert comments.fetch_top_comments("tok", "p1") == []


@respx.mock
def test_refresh_replaces_and_caps(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, score)"
                    " VALUES ('stale', ?, 'old', 'gone from reddit', 1)", (sid,))
    kids = [_c(f"c{i}", f"user{i}", f"body {i}", i) for i in range(12)]  # scores 0..11
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=_listing(kids))
    n = comments.refresh_for_sighting(db_conn, "tok", sid, "p1")
    assert n == 10
    rows = db_conn.execute("SELECT * FROM comments WHERE sighting_id=? ORDER BY score DESC",
                           (sid,)).fetchall()
    assert len(rows) == 10 and rows[0]["score"] == 11 and rows[-1]["score"] == 2
    assert not any(r["reddit_comment_id"] == "stale" for r in rows)


@respx.mock
def test_refresh_keeps_existing_on_fetch_failure(db_conn):
    sid = _insert_sighting(db_conn)
    db_conn.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, score)"
                    " VALUES ('keep', ?, 'a', 'b', 1)", (sid,))
    respx.get("https://oauth.reddit.com/comments/p1").mock(return_value=httpx.Response(500))
    assert comments.refresh_for_sighting(db_conn, "tok", sid, "p1") == 0
    assert db_conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
