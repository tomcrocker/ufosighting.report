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


def test_home_hides_non_live(client, app_db):
    seed(app_db, title="Hidden report", status="hidden_by_admin")
    seed(app_db, title="Removed report", status="removed_on_reddit")
    seed(app_db, title="Pending report", status="pending_post")
    r = client.get("/")
    assert "Hidden report" not in r.text
    assert "Removed report" not in r.text
    assert "Pending report" not in r.text


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
        seed(app_db, title=f"Report number {i:02d}", sighted_at=f"2026-07-01T05:{i:02d}:00Z")
    page1 = client.get("/").text
    page2 = client.get("/?page=2").text
    assert "Report number 29" in page1   # newest first
    assert "Report number 00" in page2   # oldest lands on page 2


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


def test_reddit_source_shows_badge(client, app_db):
    seed(app_db, title="Ingested sighting", source="reddit", reddit_post_id="zz1")
    r = client.get("/")
    assert "from r/UFOs" in r.text


def test_site_source_no_badge(client, app_db):
    seed(app_db, title="Site sighting", source="site")
    r = client.get("/")
    assert "from r/UFOs" not in r.text


def test_detail_reddit_note(client, app_db):
    sid = seed(app_db, title="Ingested detail", source="reddit", reddit_post_id="zz2")
    r = client.get(f"/sighting/{sid}")
    assert "auto-extracted" in r.text.lower()
