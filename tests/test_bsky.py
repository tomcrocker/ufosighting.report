import json

import httpx
import respx

from app import bsky
from app.config import get_settings


# ---- pure post construction (no network) -----------------------------------

def _row(**over):
    r = {"id": 42, "title": "Bright orb over the lake", "location_text": "Tofino, BC",
         "city": "Tofino", "country": "Canada", "sighted_at": "2026-07-01T05:00:00Z",
         "shape": "sphere", "description": "x" * 100, "lat": 48.8}
    r.update(over)
    return r


def test_hashtags_base_and_dynamic():
    assert bsky._hashtags(_row(shape="triangle", country="USA")) == \
        ["UFO", "UAP", "UFOsighting", "Triangle", "USA"]
    # unknown shape + no country -> base only
    assert bsky._hashtags(_row(shape="unknown", country="")) == ["UFO", "UAP", "UFOsighting"]
    # country normalised
    assert "USA" in bsky._hashtags(_row(country="United States"))


def test_build_post_text_has_fields_and_within_limit():
    text, url = bsky.build_post_text(_row())
    assert "Bright orb over the lake" in text
    assert "\U0001F4CD Tofino, BC" in text          # 📍 location
    assert "2026-07-01" in text                       # date
    assert "/sighting/42/" in url and url.startswith("http")
    assert url.split("://", 1)[-1] in text            # display URL (no scheme)
    assert "#UFO" in text and "#UFOsighting" in text
    assert len(text) <= bsky.MAX_TEXT


def test_build_post_text_truncates_long_title():
    text, _ = bsky.build_post_text(_row(title="A" * 400))
    assert len(text) <= bsky.MAX_TEXT
    assert "…" in text  # ellipsis from truncation


def test_facets_byte_offsets_are_correct():
    row = _row()
    text, url = bsky.build_post_text(row)
    facets = bsky._facets(text, url, bsky._hashtags(row))
    raw = text.encode("utf-8")
    # every facet's byte range decodes back to the exact token it points at
    seen = {}
    for f in facets:
        i = f["index"]
        frag = raw[i["byteStart"]:i["byteEnd"]].decode("utf-8")
        feat = f["features"][0]
        if feat["$type"].endswith("#tag"):
            assert frag == "#" + feat["tag"]
            seen[feat["tag"]] = frag
        else:
            assert frag == url.split("://", 1)[-1]  # link display text
    # #UFO must NOT collide with #UFOsighting
    assert seen["UFO"] == "#UFO"
    assert seen["UFOsighting"] == "#UFOsighting"


# ---- eligibility -----------------------------------------------------------

def _seed(conn, **over):
    row = {"reddit_username": "witness1", "title": "Orb over the lake",
           "description": "x" * 100, "sighted_at": "2026-07-01T05:00:00Z",
           "tz_name": "UTC", "location_text": "Tofino, BC", "country": "Canada",
           "shape": "sphere", "lat": 48.8, "lon": -124.1, "status": "live"}
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    cur = conn.execute(f"INSERT INTO sightings ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()
    return cur.lastrowid


def _media(conn, sid, thumb_key="thumbs/x.jpg"):
    conn.execute("INSERT INTO media (sighting_id, r2_key, kind, thumb_key, sort_order) "
                 "VALUES (?, 'uploads/x.mp4', 'video', ?, 0)", (sid, thumb_key))
    conn.commit()


def test_eligible_rows_filters(db_conn):
    ok = _seed(db_conn); _media(db_conn, ok)                               # media + geo + body
    no_geo = _seed(db_conn, lat=None, lon=None, description="y" * 80)      # no location -> skip
    _media(db_conn, no_geo)
    no_media = _seed(db_conn)                                              # no media -> skip
    hidden = _seed(db_conn, status="hidden_by_admin"); _media(db_conn, hidden)  # not live -> skip
    empty_body = _seed(db_conn, description="hi"); _media(db_conn, empty_body)   # body < 40 -> skip
    posted = _seed(db_conn); _media(db_conn, posted)
    db_conn.execute("UPDATE sightings SET bsky_posted_at='skipped' WHERE id=?", (posted,))
    db_conn.commit()

    ids = {r["id"] for r in bsky.eligible_rows(db_conn, limit=50)}
    assert ids == {ok}
    for skip in (no_geo, no_media, hidden, empty_body, posted):
        assert skip not in ids


def test_post_new_noop_when_disabled(db_conn, monkeypatch):
    monkeypatch.delenv("BSKY_ENABLED", raising=False)
    get_settings.cache_clear()
    sid = _seed(db_conn); _media(db_conn, sid)
    assert bsky.post_new(db_conn) == {"posted": 0, "disabled": True}


BSKY = "https://bsky.social/xrpc"


@respx.mock
def test_post_new_posts_and_marks(db_conn, monkeypatch):
    monkeypatch.setenv("BSKY_ENABLED", "1")
    monkeypatch.setenv("BSKY_HANDLE", "ufosighting.bsky.social")
    monkeypatch.setenv("BSKY_APP_PASSWORD", "app-pass")
    get_settings.cache_clear()
    monkeypatch.setattr(bsky.time, "sleep", lambda *a: None)

    respx.post(f"{BSKY}/com.atproto.server.createSession").mock(
        return_value=httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:x"}))
    respx.post(f"{BSKY}/com.atproto.repo.uploadBlob").mock(
        return_value=httpx.Response(200, json={"blob": {"$type": "blob", "ref": {"$link": "bafy"},
                                                         "mimeType": "image/jpeg", "size": 3}}))
    create = respx.post(f"{BSKY}/com.atproto.repo.createRecord").mock(
        return_value=httpx.Response(200, json={"uri": "at://did:plc:x/app.bsky.feed.post/abc"}))
    respx.get("https://media.test/thumbs/x.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0jpegbytes"))

    sid = _seed(db_conn); _media(db_conn, sid)
    result = bsky.post_new(db_conn)
    assert result == {"posted": 1}
    # row is marked posted (won't repost)
    ts = db_conn.execute("SELECT bsky_posted_at FROM sightings WHERE id=?", (sid,)).fetchone()[0]
    assert ts and ts != "skipped"
    # the record carried text + facets + image embed
    body = json.loads(create.calls[0].request.content)["record"]
    assert "Orb over the lake" in body["text"]
    assert body["facets"] and body["embed"]["$type"] == "app.bsky.embed.images"
