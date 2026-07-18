from app import analytics, auth


def _req(path, method="GET", ua="Mozilla/5.0 (Macintosh)"):
    return type("R", (), {"method": method,
                          "url": type("U", (), {"path": path})(),
                          "headers": {"user-agent": ua}})()


def test_is_countable_page_views_only():
    assert analytics.is_countable(_req("/"), 200)
    assert analytics.is_countable(_req("/sighting/5/orb-over-lake"), 200)
    assert analytics.is_countable(_req("/map"), 200)
    # not counted:
    assert not analytics.is_countable(_req("/"), 404)                 # errors
    assert not analytics.is_countable(_req("/", method="POST"), 200)  # non-GET
    assert not analytics.is_countable(_req("/static/css/site.css"), 200)
    assert not analytics.is_countable(_req("/api/pins"), 200)
    assert not analytics.is_countable(_req("/admin/analytics"), 200)
    assert not analytics.is_countable(_req("/", ua="Googlebot/2.1"), 200)
    assert not analytics.is_countable(_req("/", ua="python-requests/2.31"), 200)
    assert not analytics.is_countable(_req("/", ua=""), 200)          # no UA


def _visit(client, ip, path="/", ua="Mozilla/5.0 (Macintosh)"):
    return client.get(path, headers={"user-agent": ua, "cf-connecting-ip": ip})


def test_visit_recorded_and_deduped(client, app_db):
    _visit(client, "203.0.113.9")
    _visit(client, "203.0.113.9")             # same visitor, same day
    rows = app_db.execute("SELECT visitor, hits FROM analytics_visits").fetchall()
    assert len(rows) == 1 and rows[0]["hits"] == 2   # 1 unique, 2 page views


def test_distinct_ips_are_distinct_visitors(client, app_db):
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        _visit(client, ip)
    assert app_db.execute("SELECT COUNT(*) FROM analytics_visits").fetchone()[0] == 3


def test_bots_assets_api_not_counted(client, app_db):
    _visit(client, "9.9.9.9", ua="Googlebot/2.1")
    _visit(client, "9.9.9.8", path="/static/css/site.css")
    _visit(client, "9.9.9.7", path="/api/pins")
    assert app_db.execute("SELECT COUNT(*) FROM analytics_visits").fetchone()[0] == 0


def test_no_raw_ip_stored(client, app_db):
    _visit(client, "198.51.100.5")
    visitor = app_db.execute("SELECT visitor FROM analytics_visits").fetchone()[0]
    assert "198.51.100.5" not in visitor and len(visitor) == 16


def test_summary_shape(client, app_db):
    _visit(client, "1.2.3.4"); _visit(client, "5.6.7.8"); _visit(client, "1.2.3.4")
    s = analytics.summary(app_db)
    assert s["today"] == 2 and s["views30"] == 3
    assert s["daily"][0]["visitors"] == 2 and s["daily"][0]["views"] == 3


def test_analytics_page_is_admin_gated(client, app_db):
    assert client.get("/admin/analytics").status_code == 404      # anonymous
    client.cookies.set("sid", auth.create_session(app_db, "tmosh", "tok", 3600))
    _visit(client, "7.7.7.7")
    r = client.get("/admin/analytics")
    assert r.status_code == 200 and "Visitors" in r.text
