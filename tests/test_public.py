import json


def seed(app_db, **over):
    row = {
        "reddit_username": "witness1",
        "title": "Bright orb over the lake",
        "description": "A detailed story about the orb sighting. " * 5,
        "sighted_at": "2026-07-01T05:00:00Z",
        "tz_name": "America/Vancouver",
        "location_text": "Lake Cowichan, BC",
        "country": "Canada",
        "shape": "sphere",
        "lat": 48.8,
        "lon": -124.1,
        "status": "live",
        "movement": json.dumps(["hovering"]),
        "num_objects": "2",
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    cur = app_db.execute(f"INSERT INTO sightings ({cols}) VALUES ({marks})", list(row.values()))
    app_db.commit()
    return cur.lastrowid


def add_media(app_db, sighting_id, kind="image"):
    app_db.execute(
        "INSERT INTO media (sighting_id, r2_key, thumb_key, kind) VALUES (?,?,?,?)",
        (sighting_id, f"uploads/2026/07/{'e' * 32}.jpg", f"thumbs/2026/07/{'e' * 32}.jpg", kind),
    )
    app_db.commit()


def test_home_renders_live_sightings(client, app_db):
    seed(app_db)
    r = client.get("/")
    assert r.status_code == 200
    assert "Bright orb over the lake" in r.text


def test_home_hides_admin_hidden_and_pending(client, app_db):
    seed(app_db, title="Hidden report", status="hidden_by_admin")
    seed(app_db, title="Pending report", status="pending_post")
    seed(app_db, title="Awaiting verify", status="pending_verify")
    seed(app_db, title="Awaiting review", status="pending_review")
    r = client.get("/")
    assert "Hidden report" not in r.text
    assert "Pending report" not in r.text
    assert "Awaiting verify" not in r.text
    assert "Awaiting review" not in r.text


def test_removed_on_reddit_stays_visible(client, app_db):
    # spam/automod-pending posts aren't a deletion and flip back to live if
    # approved, so they stay in the public archive
    m = seed(app_db, title="Mod removed sighting", status="removed_on_reddit")
    home = client.get("/").text
    assert "Mod removed sighting" in home
    detail_m = client.get(f"/sighting/{m}").text
    assert "removed on Reddit" in detail_m and "preserved here" in detail_m


def test_author_deleted_hidden_from_public(client, app_db):
    # Reddit Data API compliance: a post the author deleted is honored as
    # deleted — pulled from the public archive and 410 (Gone) so Google
    # de-indexes it, while the row is retained privately.
    d = seed(app_db, title="Author deleted sighting", status="deleted_by_user")
    assert "Author deleted sighting" not in client.get("/").text
    assert client.get(f"/sighting/{d}", follow_redirects=False).status_code == 410


def test_author_deleted_visible_to_admin(client, app_db):
    from app import auth
    d = seed(app_db, title="Author deleted sighting", status="deleted_by_user")
    admin_sid = auth.create_session(app_db, "tmosh", "tok-admin", 3600)
    client.cookies.set("sid", admin_sid)
    detail = client.get(f"/sighting/{d}")
    assert detail.status_code == 200
    assert "retained here for moderators only" in detail.text


def test_mod_removed_hidden_from_gallery(client, app_db):
    # a genuine mod removal is pulled from the public archive (still stored)
    seed(app_db, title="Mod removed spam post", status="removed_by_mod")
    assert "Mod removed spam post" not in client.get("/").text


def test_mod_removed_detail_returns_410(client, app_db):
    sid = seed(app_db, status="removed_by_mod")
    # 410 Gone so Google de-indexes it cleanly (it was public + indexed)
    assert client.get(f"/sighting/{sid}", follow_redirects=False).status_code == 410


def test_mod_removed_visible_to_admin(client, app_db):
    from app import auth
    sid = seed(app_db, status="removed_by_mod")
    admin_sid = auth.create_session(app_db, "tmosh", "tok-admin", 3600)
    client.cookies.set("sid", admin_sid)
    assert client.get(f"/sighting/{sid}").status_code == 200


def test_pins_include_archived_and_accept_filters(client, app_db):
    kept = seed(app_db, title="Archived pinned", status="removed_on_reddit",
                lat=10.0, lon=10.0,
                sighted_at="2026-07-01T05:00:00Z", shape="triangle")
    seed(app_db, title="Out of range", status="live", lat=20.0, lon=20.0,
         sighted_at="2026-01-01T05:00:00Z", shape="sphere")
    pins = client.get("/api/pins?from=2026-06-01&shape=triangle").json()["pins"]
    assert len(pins) == 1 and pins[0][0] == kept


def test_shape_filter(client, app_db):
    seed(app_db, title="Sphere report", shape="sphere")
    seed(app_db, title="Triangle report", shape="triangle")
    r = client.get("/?shape=triangle")
    assert "Triangle report" in r.text
    assert "Sphere report" not in r.text


def test_country_filter(client, app_db):
    seed(app_db, title="Canada report", country="Canada")
    seed(app_db, title="USA report", country="United States")
    r = client.get("/?country=Canada")
    assert "Canada report" in r.text
    assert "USA report" not in r.text


def test_date_filter(client, app_db):
    seed(app_db, title="July report", sighted_at="2026-07-01T05:00:00Z")
    seed(app_db, title="May report", sighted_at="2026-05-01T05:00:00Z")
    r = client.get("/?from=2026-06-01&to=2026-07-31")
    assert "July report" in r.text
    assert "May report" not in r.text


def test_media_filter(client, app_db):
    with_video = seed(app_db, title="Video report")
    add_media(app_db, with_video, kind="video")
    seed(app_db, title="No media report")
    r = client.get("/?media=video")
    assert "Video report" in r.text
    assert "No media report" not in r.text


def test_featured_sorts_first(client, app_db):
    seed(app_db, title="Ordinary newer", sighted_at="2026-07-05T05:00:00Z")
    seed(app_db, title="Featured older", sighted_at="2026-06-01T05:00:00Z", featured=1)
    r = client.get("/")
    assert r.text.index("Featured older") < r.text.index("Ordinary newer")


def test_pagination(client, app_db):
    for i in range(30):
        seed(app_db, title=f"Report number {i:02d}", created_at=f"2026-07-01T05:{i:02d}:00Z")
    page1 = client.get("/").text
    page2 = client.get("/?page=2").text
    assert "Report number 29" in page1   # latest posted first
    assert "Report number 00" in page2   # earliest lands on page 2


def test_detail_shows_structured_fields(client, app_db):
    sid = seed(app_db, distance="above the trees", apparent_size="golf ball",
               has_plume="unsure", witnesses=2,
               sensors=json.dumps(["infrared"]),
               witness_background=json.dumps(["pilot"]),
               reddit_post_id="1abcde", reddit_score=42, reddit_num_comments=7)
    r = client.get(f"/sighting/{sid}/bright-orb-over-the-lake")
    assert r.status_code == 200
    for text in ("above the trees", "golf ball", "hovering", "infrared", "pilot",
                 "u/witness1", "reddit.com/comments/1abcde"):
        assert text in r.text


def test_detail_shows_reddit_posted_date(client, app_db):
    sid = seed(app_db, source="reddit", reddit_post_id="1abcde",
               reddit_posted_at="2026-07-15T18:24:00Z")
    r = client.get(f"/sighting/{sid}/bright-orb-over-the-lake")
    assert "Posted to Reddit" in r.text
    assert "2026-07-15" in r.text  # ISO — consistent date mask sitewide


def test_detail_hides_reddit_posted_date_when_absent(client, app_db):
    sid = seed(app_db)  # no reddit_posted_at
    r = client.get(f"/sighting/{sid}/bright-orb-over-the-lake")
    assert "Posted to Reddit" not in r.text


def test_detail_slug_optional(client, app_db):
    sid = seed(app_db)
    assert client.get(f"/sighting/{sid}").status_code == 200


def test_detail_404_for_hidden_unless_admin(client, app_db):
    from app import auth
    sid = seed(app_db, status="hidden_by_admin")
    assert client.get(f"/sighting/{sid}").status_code == 404
    admin_sid = auth.create_session(app_db, "tmosh", "tok-admin", 3600)
    client.cookies.set("sid", admin_sid)
    assert client.get(f"/sighting/{sid}").status_code == 200


def test_detail_404_unknown(client):
    assert client.get("/sighting/9999").status_code == 404


def test_shape_filter_matches_text_mentions(client, app_db):
    # ingested posts rarely carry a structured shape, so the chip matches
    # MENTIONS in the text too, OR the structured field. (This exercises the
    # SQL/FTS fallback: exact word + plural prefix; adjective forms like
    # "triangular" are covered by Meili synonyms on the live path.)
    seed(app_db, title="Dark craft over the ridge", shape=None,
         description="A silent black triangle drifted overhead. " * 4)
    seed(app_db, title="Plural mention row", shape=None,
         description="Two glowing triangles crossed the valley. " * 4)
    seed(app_db, title="Structured shape row", shape="triangle",
         description="It moved strangely and vanished. " * 4)
    seed(app_db, title="Unrelated orb story", shape=None,
         description="A glowing ball hovered near the pier. " * 4)
    r = client.get("/?shape=triangle").text
    assert "Dark craft over the ridge" in r
    assert "Plural mention row" in r
    assert "Structured shape row" in r
    assert "Unrelated orb story" not in r


def test_reddit_source_shows_badge(client, app_db):
    seed(app_db, title="Ingested sighting", source="reddit", reddit_post_id="zz1")
    r = client.get("/")
    assert 'class="src-badge"' in r.text


def test_site_source_no_badge(client, app_db):
    # the badge itself, not the phrase — meta description mentions r/UFOs
    seed(app_db, title="Site sighting", source="site")
    r = client.get("/")
    assert 'class="src-badge"' not in r.text


def test_detail_reddit_note(client, app_db):
    sid = seed(app_db, title="Ingested detail", source="reddit", reddit_post_id="zz2")
    r = client.get(f"/sighting/{sid}")
    assert "auto-extracted" in r.text.lower()


def test_sort_default_latest_first(client, app_db):
    # "Latest" = most recently posted/added (created_at), NOT sighting date:
    # the later-posted row wins even though its sighting is older
    seed(app_db, title="Posted earlier", created_at="2026-07-01T00:00:00Z",
         sighted_at="2026-07-05T05:00:00Z")
    seed(app_db, title="Posted later", created_at="2026-07-10T00:00:00Z",
         sighted_at="2026-06-01T05:00:00Z")
    text = client.get("/").text
    assert text.index("Posted later") < text.index("Posted earlier")


def test_sort_oldest_first(client, app_db):
    seed(app_db, title="Older entry", sighted_at="2026-06-01T05:00:00Z")
    seed(app_db, title="Newer entry", sighted_at="2026-07-05T05:00:00Z")
    text = client.get("/?sort=old").text
    assert text.index("Older entry") < text.index("Newer entry")


def test_sort_top_by_score(client, app_db):
    seed(app_db, title="Low score entry", reddit_score=3, sighted_at="2026-07-05T05:00:00Z")
    seed(app_db, title="High score entry", reddit_score=99, sighted_at="2026-06-01T05:00:00Z")
    text = client.get("/?sort=top&t=all").text
    assert text.index("High score entry") < text.index("Low score entry")


def test_sort_top_time_window(client, app_db):
    # sighted long ago — excluded from top past week
    seed(app_db, title="Ancient banger", reddit_score=999, sighted_at="2020-01-01T00:00:00Z")
    seed(app_db, title="Recent modest", reddit_score=5,
         sighted_at=__import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    text = client.get("/?sort=top&t=week").text
    assert "Recent modest" in text and "Ancient banger" not in text


def test_detail_shows_top_comments(client, app_db):
    sid = seed(app_db, reddit_post_id="1cmt01")
    app_db.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, "
                   "score, permalink) VALUES ('c1', ?, 'alice', 'that is **wow** footage', 42, "
                   "'/r/UFOs/comments/1cmt01/x/c1/')", (sid,))
    app_db.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, body, "
                   "score, permalink) VALUES ('c2', ?, 'bob', 'starlink again', 7, '')", (sid,))
    app_db.commit()
    r = client.get(f"/sighting/{sid}")
    assert "Top comments on Reddit" in r.text
    assert "<strong>wow</strong>" in r.text          # reddit_md rendered
    assert "u/alice" in r.text and "u/bob" in r.text
    assert r.text.index("u/alice") < r.text.index("u/bob")  # score order
    assert "reddit.com/r/UFOs/comments/1cmt01/x/c1" in r.text


def test_detail_no_comments_section_when_empty(client, app_db):
    sid = seed(app_db)
    assert "Top comments on Reddit" not in client.get(f"/sighting/{sid}").text


def test_detail_hides_bot_comments(client, app_db):
    # bot comments already stored (from a pre-skip-list backfill) must not render
    sid = seed(app_db, reddit_post_id="1cmt02")
    for cid, author, body, score in [
        ("c1", "alice", "real observation", 42),
        ("c2", "CollapseBot", "This thread has been collapsed", 999),
        ("c3", "ufomodbot", "Removed: rule 3", 500),
    ]:
        app_db.execute("INSERT INTO comments (reddit_comment_id, sighting_id, author, "
                       "body, score, permalink) VALUES (?,?,?,?,?,'')",
                       (cid, sid, author, body, score))
    app_db.commit()
    r = client.get(f"/sighting/{sid}")
    assert "u/alice" in r.text and "real observation" in r.text
    assert "CollapseBot" not in r.text and "collapsed" not in r.text
    assert "ufomodbot" not in r.text


def test_guide_page(client):
    r = client.get("/guide")
    assert r.status_code == 200
    for text in ("How to report a UFO sighting", "u/modbot", "6 hours",
                 "commonly misidentified", "byte-for-byte untouched",
                 "five observables", "/investigate"):
        assert text in r.text, text


# --- SEO ---

def test_detail_bare_id_redirects_to_slug(client, app_db):
    sid = seed(app_db, title="Redirect me sighting")
    r = client.get(f"/sighting/{sid}", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == f"/sighting/{sid}/redirect-me-sighting"
    r = client.get(f"/sighting/{sid}/wrong-stale-slug", follow_redirects=False)
    assert r.status_code == 301


def test_detail_seo_head(client, app_db):
    import json as _json
    import re as _re
    sid = seed(app_db, title="Schema check orb", country="Canada", city="Victoria")
    r = client.get(f"/sighting/{sid}/schema-check-orb")
    assert 'rel="canonical"' in r.text
    assert f"/sighting/{sid}/schema-check-orb" in r.text
    blocks = _re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, _re.S)
    assert len(blocks) >= 2  # Article + BreadcrumbList
    parsed = [_json.loads(b) for b in blocks]
    types = {p["@type"] for p in parsed}
    assert "Article" in types and "BreadcrumbList" in types
    art = next(p for p in parsed if p["@type"] == "Article")
    assert art["headline"] == "Schema check orb"
    assert art["author"]["name"] == "u/witness1"
    # geo present (seed provides lat/lon)
    assert art["contentLocation"]["geo"]["latitude"]
    assert "More sightings in Canada" in r.text


def test_index_canonical_strips_search_query(client, app_db):
    r = client.get("/?q=orbs&country=Canada")
    assert 'rel="canonical" href="http://testserver/?country=Canada"' in r.text
    r = client.get("/")
    assert 'rel="canonical" href="http://testserver/"' in r.text


def test_guide_faq_schema(client):
    import json as _json
    import re as _re
    r = client.get("/guide")
    blocks = _re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, _re.S)
    faq = next(_json.loads(b) for b in blocks if '"FAQPage"' in b)
    assert len(faq["mainEntity"]) == 5


def test_sitemap_lastmod(client, app_db):
    seed(app_db)
    r = client.get("/sitemap.xml")
    assert "<lastmod>" in r.text


def test_submitted_noindex_submit_indexable(client, app_db, monkeypatch):
    r = client.get("/submit")
    assert "noindex" not in r.text
    assert 'rel="canonical" href="http://testserver/submit"' in r.text


def test_custom_404_page(client):
    r = client.get("/sighting/999999")
    assert r.status_code == 404
    assert "abducted" in r.text and "<html" in r.text


def test_api_404_stays_json(client):
    r = client.get("/api/reverse?lat=999&lon=0")
    assert r.status_code == 400
    assert r.json()["detail"]


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_feed_xml(client, app_db):
    seed(app_db, title="Feed check sighting & more")
    r = client.get("/feed.xml")
    assert r.status_code == 200
    assert "application/rss+xml" in r.headers["content-type"]
    assert "Feed check sighting &amp; more" in r.text
    assert "<pubDate>" in r.text


def test_hero_stats(client, app_db):
    seed(app_db)
    r = client.get("/")
    assert "sightings archived" in r.text and "/feed.xml" in r.text


# --- sighting correlation ---

def test_related_sightings_window_and_ranking(client, app_db):
    base = seed(app_db, title="Anchor orb report", sighted_at="2026-07-01T06:00:00Z",
                lat=48.43, lon=-123.36)
    near = seed(app_db, title="Nearby same night", sighted_at="2026-07-01T08:00:00Z",
                lat=48.65, lon=-123.40)     # ~25 km
    farther = seed(app_db, title="Vancouver same night", sighted_at="2026-07-01T05:00:00Z",
                   lat=49.28, lon=-123.12)  # ~93 km
    seed(app_db, title="Too far away", sighted_at="2026-07-01T06:30:00Z",
         lat=51.05, lon=-114.07)            # Calgary, ~700 km
    seed(app_db, title="Too long ago", sighted_at="2026-06-20T06:00:00Z",
         lat=48.45, lon=-123.37)
    hidden = seed(app_db, title="Hidden nearby", sighted_at="2026-07-01T07:00:00Z",
                  lat=48.44, lon=-123.37, status="hidden_by_admin")
    r = client.get(f"/sighting/{base}/anchor-orb-report")
    assert "Possibly related reports" in r.text
    assert "Nearby same night" in r.text
    assert "Vancouver same night" in r.text
    assert "Too far away" not in r.text
    assert "Too long ago" not in r.text
    assert "Hidden nearby" not in r.text
    # ranked by distance: nearby first
    assert r.text.index("Nearby same night") < r.text.index("Vancouver same night")
    assert "km away" in r.text


def test_related_absent_without_geo_or_matches(client, app_db):
    lone = seed(app_db, title="Lonely sighting", lat=None, lon=None)
    r = client.get(f"/sighting/{lone}/lonely-sighting")
    assert "Possibly related" not in r.text


# --- sky context links ---

def test_sky_context_links(client, app_db):
    import json as _json
    sid = seed(app_db, title="Sky context check", sighted_at="2026-07-01T06:00:00Z",
               lat=48.43, lon=-123.36)
    app_db.execute("INSERT INTO media (sighting_id, r2_key, kind, exif_json) VALUES "
                   "(?, 'uploads/sky.jpg', 'image', ?)",
                   (sid, _json.dumps({"compass_deg": 205.6, "compass_ref": "true"})))
    app_db.commit()
    r = client.get(f"/sighting/{sid}/sky-context-check")
    assert "What was in the sky" in r.text
    assert "globe.adsbexchange.com" in r.text and "replay=2026-07-01-06:00" in r.text
    assert "flightradar24.com/48.43,-123.36/9" in r.text
    assert "heavens-above.com" in r.text
    assert "in-the-sky.org/skymap.php?year=2026&amp;month=7&amp;day=1" in r.text
    assert "205.6" in r.text and "SSW" in r.text  # camera heading + compass name


def test_sky_context_absent_without_geo(client, app_db):
    sid = seed(app_db, title="No geo no sky", lat=None, lon=None)
    r = client.get(f"/sighting/{sid}/no-geo-no-sky")
    assert "What was in the sky" not in r.text


def test_sky_panel_renders_computed_satellites(client, app_db):
    import json as _json
    sid = seed(app_db, title="Sat panel check", lat=48.4, lon=-123.3,
               sighted_at="2026-07-01T06:00:00Z")
    app_db.execute("UPDATE sightings SET sky_events=? WHERE id=?", (_json.dumps({
        "checked": True, "catalog_date": "2026-07-01", "visibility_filtered": True,
        "bright": [], "starlink_visible": 0,
        "trains": [{"batch": "26042", "count": 17, "az": "NW/SE", "time": "05:58"}],
    }), sid))
    app_db.commit()
    r = client.get(f"/sighting/{sid}/sat-panel-check")
    assert "Starlink train overhead" in r.text and "17 satellites" in r.text


def test_sky_panel_negative_result(client, app_db):
    import json as _json
    sid = seed(app_db, title="Sat negative check", lat=48.4, lon=-123.3)
    app_db.execute("UPDATE sightings SET sky_events=? WHERE id=?", (_json.dumps({
        "checked": True, "catalog_date": "2026-07-01", "visibility_filtered": True,
        "bright": [], "starlink_visible": 0, "trains": [],
    }), sid))
    app_db.commit()
    r = client.get(f"/sighting/{sid}/sat-negative-check")
    assert "No bright satellites were visible overhead" in r.text


def test_anonymous_page_gated_off_by_default(client):
    r = client.get("/anonymous")
    assert r.status_code == 200
    assert "Share footage anonymously" in r.text
    assert "being finalized" in r.text          # onion withheld
    assert "4hqzw2" not in r.text                # real onion not shown
    assert "noindex" in r.text                   # not indexable while off


def test_anonymous_page_shows_onion_when_enabled(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ANONYMOUS_ENABLED", "true")
    monkeypatch.setenv("ANONYMOUS_ONION", "testonionaddr7xyz.onion")
    get_settings.cache_clear()
    r = client.get("/anonymous")
    assert "testonionaddr7xyz.onion" in r.text
    assert "noindex" not in r.text
    assert "r/UFOs" in r.text and "moderation team" in r.text


def test_anonymous_in_sitemap_only_when_enabled(client, monkeypatch):
    from app.config import get_settings
    assert "/anonymous" not in client.get("/sitemap.xml").text
    monkeypatch.setenv("ANONYMOUS_ENABLED", "1")
    get_settings.cache_clear()
    assert "/anonymous" in client.get("/sitemap.xml").text


def test_ga_absent_by_default(client):
    assert "googletagmanager.com" not in client.get("/").text


def test_ga_renders_when_configured_but_not_on_anonymous(client, monkeypatch):
    from app.web import templates
    monkeypatch.setitem(templates.env.globals, "ga_id", "G-TESTID123")
    home = client.get("/")
    assert "googletagmanager.com/gtag/js?id=G-TESTID123" in home.text
    # the anonymous-submission page suppresses analytics for source privacy
    anon = client.get("/anonymous")
    assert "googletagmanager.com" not in anon.text


def test_video_object_uses_post_date_and_duration(client, app_db):
    sid = seed(app_db, source="reddit", reddit_post_id="1vid",
               reddit_posted_at="2026-07-09T21:30:00Z",
               created_at="2026-07-11T00:00:00Z", duration_seconds=125)
    app_db.execute(
        "INSERT INTO media (sighting_id, r2_key, kind, thumb_key, sort_order) "
        "VALUES (?, 'uploads/2026/07/x.mp4', 'video', 'thumbs/x.jpg', 0)", (sid,))
    app_db.commit()
    html = client.get(f"/sighting/{sid}").text
    assert '"@type": "VideoObject"' in html
    assert '"@type": "Article"' not in html          # video page: video is the primary entity
    assert '"duration": "PT125S"' in html
    # uploadDate is the real Reddit post date, not the ingest/created_at date
    assert '"uploadDate": "2026-07-09T21:30:00Z"' in html
    block = html.split('"VideoObject"', 1)[1].split("</script>", 1)[0]
    assert "2026-07-11" not in block  # created_at must NOT be the uploadDate
    assert '"contentLocation"' in block and '"latitude": 48.8' in block  # geo preserved


def test_text_sighting_keeps_article_schema(client, app_db):
    sid = seed(app_db)  # no media -> not a video watch page
    html = client.get(f"/sighting/{sid}").text
    assert '"@type": "Article"' in html
    assert '"@type": "VideoObject"' not in html


def test_www_host_redirects_to_apex(client):
    r = client.get("/guide", headers={"host": "www.testserver"}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "http://testserver/guide"


def test_www_redirect_preserves_query(client):
    r = client.get("/?q=orb&t=week", headers={"host": "www.testserver"},
                   follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "http://testserver/?q=orb&t=week"


def test_apex_host_not_redirected(client):
    r = client.get("/guide", headers={"host": "testserver"}, follow_redirects=False)
    assert r.status_code == 200
