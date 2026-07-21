import json
import math
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app import auth, db, helpers, mediameta, r2, search as meili
from app.config import get_settings
from app.investigate_data import ENTRIES as INVESTIGATE_ENTRIES
from app.web import current_user, is_admin, templates

router = APIRouter()

PER_PAGE = 24

SORTS = ("new", "old", "top")
TOP_WINDOW_HOURS = {"day": 24, "week": 24 * 7, "month": 24 * 30, "year": 24 * 365, "all": None}

# Archive philosophy: entries persist even when the Reddit post is removed
# (by mods) or deleted (by the author) — reddit status is shown as provenance,
# not used for visibility. hidden_by_admin is the site's own kill switch.
PUBLIC_STATUSES = ("live", "deleted_by_user", "removed_on_reddit")
PUBLIC_STATUSES_SQL = "('live', 'deleted_by_user', 'removed_on_reddit')"


def query_sightings(conn, *, shape=None, country=None, date_from=None, date_to=None,
                    media_kind=None, sort="new", top_window="all", page=1, per_page=PER_PAGE):
    where = [f"s.status IN {PUBLIC_STATUSES_SQL}"]
    args: list = []
    if shape:
        # mirror the Meili behaviour: match shape MENTIONS in the text (FTS,
        # prefix so "triangle" finds "triangular") OR the structured field
        where.append("(s.shape = ? OR s.id IN "
                     "(SELECT rowid FROM sightings_fts WHERE sightings_fts MATCH ?))")
        args.extend([shape, f'"{shape}"*'])
    if country:
        where.append("s.country = ? COLLATE NOCASE")
        args.append(country)
    if date_from:
        where.append("s.sighted_at >= ?")
        args.append(date_from + "T00:00:00Z")
    if date_to:
        where.append("s.sighted_at <= ?")
        args.append(date_to + "T23:59:59Z")
    if media_kind in ("image", "video"):
        where.append("EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id AND m.kind = ?)")
        args.append(media_kind)
    if sort == "top":
        hours = TOP_WINDOW_HOURS.get(top_window)
        if hours:
            where.append("s.sighted_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)")
            args.append(f"-{hours} hours")
        order = "s.featured DESC, s.reddit_score DESC, s.sighted_at DESC"
    elif sort == "old":
        order = "s.featured DESC, s.sighted_at ASC"
    else:  # "new"/"latest": most recently added first (Meili path uses post_order)
        order = "s.featured DESC, s.created_at DESC"
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM sightings s WHERE {clause}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT s.*,
              (SELECT m.thumb_key FROM media m WHERE m.sighting_id = s.id
                 ORDER BY m.sort_order LIMIT 1) AS thumb_key,
              (SELECT m.kind FROM media m WHERE m.sighting_id = s.id
                 ORDER BY m.sort_order LIMIT 1) AS first_kind
            FROM sightings s WHERE {clause}
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
        args + [per_page, (page - 1) * per_page],
    ).fetchall()
    return rows, total


def card(row) -> dict:
    d = dict(row)
    d["slug"] = helpers.slugify(row["title"])
    d["thumb_url"] = r2.public_url(row["thumb_key"]) if row["thumb_key"] else None
    d["kind"] = row["first_kind"]
    return d


def hydrate_cards(conn, ids: list[int]) -> list[dict]:
    """Fetch card rows for Meili-ranked ids, preserving Meili's order."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT s.*,
              (SELECT m.thumb_key FROM media m WHERE m.sighting_id = s.id
                 ORDER BY m.sort_order LIMIT 1) AS thumb_key,
              (SELECT m.kind FROM media m WHERE m.sighting_id = s.id
                 ORDER BY m.sort_order LIMIT 1) AS first_kind
            FROM sightings s WHERE s.id IN ({marks})""",
        list(ids),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [card(by_id[i]) for i in ids if i in by_id]


@router.get("/")
def index(
    request: Request,
    q: str = "",
    shape: str = "",
    country: str = "",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    media: str = "",
    sort: str = "",
    t: str = "all",
    page: int = 1,
    conn=Depends(db.get_db),
    user=Depends(current_user),
):
    page = max(1, page)
    q = q.strip()
    # text queries default to Meili relevance; browsing defaults to newest
    if sort not in SORTS:
        sort = "relevance" if q else "new"
    if t not in TOP_WINDOW_HOURS:
        t = "all"
    hit = meili.search_ids(
        q=q, shape=shape or None, country=country or None,
        date_from=date_from or None, date_to=date_to or None,
        media_kind=media or None, sort=sort, top_window=t,
        page=page, per_page=PER_PAGE,
    )
    if hit is not None:
        cards_list, total = hydrate_cards(conn, hit["ids"]), hit["total"]
    elif q:  # FTS5 fallback for text queries (meili disabled or down)
        # quoted prefix terms: safe against FTS syntax, matches plurals (orb→orbs)
        match = " ".join('"' + term.replace('"', "") + '"*' for term in q.split())
        rows = conn.execute(
            f"""SELECT s.*,
                  (SELECT m.thumb_key FROM media m WHERE m.sighting_id = s.id
                     ORDER BY m.sort_order LIMIT 1) AS thumb_key,
                  (SELECT m.kind FROM media m WHERE m.sighting_id = s.id
                     ORDER BY m.sort_order LIMIT 1) AS first_kind
               FROM sightings_fts f
               JOIN sightings s ON s.id = f.rowid
               WHERE sightings_fts MATCH ? AND s.status IN {PUBLIC_STATUSES_SQL}
               ORDER BY f.rank LIMIT ?""",
            (match, PER_PAGE),
        ).fetchall()
        cards_list, total = [card(r) for r in rows], len(rows)
    else:  # SQL fallback for plain browsing
        rows, total = query_sightings(
            conn, shape=shape or None, country=country or None,
            date_from=date_from or None, date_to=date_to or None,
            media_kind=media or None, sort=sort if sort in SORTS else "new",
            top_window=t, page=page,
        )
        cards_list = [card(r) for r in rows]
    stats = conn.execute(
        f"""SELECT COUNT(*) AS n, MAX(created_at) AS latest FROM sightings
            WHERE status IN {PUBLIC_STATUSES_SQL}"""
    ).fetchone()
    countries = [
        r["country"] for r in conn.execute(
            f"""SELECT DISTINCT country FROM sightings
               WHERE status IN {PUBLIC_STATUSES_SQL} AND country IS NOT NULL AND country != ''
               ORDER BY country"""
        )
    ]
    filters = {"q": q, "shape": shape, "country": country, "from": date_from,
               "to": date_to, "media": media}
    if sort not in ("new", "relevance"):
        filters["sort"] = sort
    if sort == "top" and t != "all":
        filters["t"] = t
    qs = urllib.parse.urlencode({k: v for k, v in filters.items() if v})
    base = get_settings().base_url
    canon_params = {k: v for k, v in filters.items() if v and k != "q"}
    if page > 1:
        canon_params["page"] = page
    canon_qs = urllib.parse.urlencode(canon_params)
    return templates.TemplateResponse(
        request, "index.html",
        {
            "user": user,
            "canonical": f"{base}/" + (f"?{canon_qs}" if canon_qs else ""),
            "cards": cards_list,
            "f": filters,
            "q": q,
            "sort": sort,
            "t": t,
            "top_windows": list(TOP_WINDOW_HOURS),
            "countries": countries,
            "shapes": helpers.SHAPES,
            "page": page,
            "pages": max(1, math.ceil(total / PER_PAGE)),
            "total": total,
            "grand_total": stats["n"],
            "latest_at": stats["latest"],
            "qs": qs,
        },
    )


RELATED_KM = 100
RELATED_HOURS = 48


def related_sightings(conn, row, limit: int = 5) -> list[dict]:
    """Independent reports near in space AND time — the strongest signal a
    sighting has. Bounding-box prefilter, exact haversine ranking."""
    from datetime import datetime, timedelta, timezone
    from math import cos, radians
    if row["lat"] is None:
        return []
    ts = datetime.strptime(row["sighted_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    t0 = (ts - timedelta(hours=RELATED_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t1 = (ts + timedelta(hours=RELATED_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dlat = RELATED_KM / 111.0
    dlon = RELATED_KM / (111.0 * max(0.2, cos(radians(row["lat"]))))
    rows = conn.execute(
        f"""SELECT id, title, sighted_at, lat, lon, city, country FROM sightings
            WHERE status IN {PUBLIC_STATUSES_SQL} AND id != ?
              AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
              AND sighted_at BETWEEN ? AND ?""",
        (row["id"], row["lat"] - dlat, row["lat"] + dlat,
         row["lon"] - dlon, row["lon"] + dlon, t0, t1),
    ).fetchall()
    out = []
    for r in rows:
        km = helpers.haversine_km(row["lat"], row["lon"], r["lat"], r["lon"])
        if km > RELATED_KM:
            continue
        other = datetime.strptime(r["sighted_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        dh = (other - ts).total_seconds() / 3600
        if abs(dh) < 1:
            when = "within the hour"
        else:
            when = f"{abs(dh):.0f} h {'later' if dh > 0 else 'earlier'}"
        out.append({"id": r["id"], "title": r["title"],
                    "slug": helpers.slugify(r["title"]),
                    "km": round(km), "when": when,
                    "place": r["city"] or r["country"] or ""})
    out.sort(key=lambda x: x["km"])
    return out[:limit]


@router.get("/sighting/{sighting_id}")
@router.get("/sighting/{sighting_id}/{slug}")
def detail(
    request: Request,
    sighting_id: int,
    slug: str = "",
    conn=Depends(db.get_db),
    user=Depends(current_user),
):
    row = conn.execute("SELECT * FROM sightings WHERE id=?", (sighting_id,)).fetchone()
    admin = is_admin(user)
    if row is None:
        raise HTTPException(status_code=404)
    if row["status"] not in PUBLIC_STATUSES and not admin:
        # mod-removed posts were public + indexed, so 410 (Gone) tells Google
        # they're intentionally removed and de-indexes them cleanly; other
        # non-public states (pending, admin-hidden) just 404.
        raise HTTPException(status_code=410 if row["status"] == "removed_by_mod" else 404)
    canonical_slug = helpers.slugify(row["title"])
    if slug != canonical_slug:
        # one canonical URL per sighting — /sighting/{id} and stale slugs 301
        return Response(status_code=301, headers={
            "Location": f"/sighting/{sighting_id}/{canonical_slug}"})
    media = conn.execute(
        "SELECT * FROM media WHERE sighting_id=? ORDER BY sort_order", (sighting_id,)
    ).fetchall()
    s = dict(row)
    s["slug"] = helpers.slugify(row["title"])
    s["sighted_local"] = helpers.from_utc(row["sighted_at"], row["tz_name"])
    for field in ("movement", "sensors", "witness_background"):
        s[field] = json.loads(row[field]) if row[field] else []
    media_items = []
    for m in media:
        meta = json.loads(m["exif_json"]) if m["exif_json"] else {}
        prefs = json.loads(m["exif_prefs"]) if m["exif_prefs"] else {}
        loc_excluded = not prefs.get("location", True)
        media_items.append({
            "url": r2.public_url(m["r2_key"]),
            "thumb_url": r2.public_url(m["thumb_key"]) if m["thumb_key"] else None,
            # HEIC originals get a JPEG derivative for browsers that can't
            # render them; the original stays available via the download link
            "display_url": r2.public_url(m["display_key"]) if m["display_key"] else None,
            "kind": m["kind"],
            "ext": m["r2_key"].rsplit(".", 1)[-1],
            "size_mb": round(m["size_bytes"] / 1048576, 1) if m["size_bytes"] else None,
            # GPS from EXIF can expose a home address — only shown when the
            # reporter did NOT ask to obscure the location
            "meta_rows": mediameta.public_rows(
                meta, include_gps=not row["location_obscured"] and not loc_excluded),
            # originality signal — only meaningful for site uploads (ingested
            # Reddit media is already transcoded by Reddit, so we don't judge it)
            "provenance": mediameta.provenance(meta) if row["source"] == "site" else None,
            # reporter excluded location: originals with embedded GPS are
            # withheld; the EXIF-free display derivative is downloadable
            "download_original": not (loc_excluded and m["kind"] == "image"),
        })
    reddit_url = None
    if row["reddit_post_id"]:
        # subreddit-agnostic permalink: reddit redirects /comments/{id}/ to the
        # canonical URL, correct for both bot posts (SUBREDDIT) and ingested
        # posts (INGEST_SUBREDDIT)
        reddit_url = f"https://www.reddit.com/comments/{row['reddit_post_id']}/"
    # filter bot accounts at display time too, so comments stored before the
    # skip-list existed (backfill manifests) never surface
    from app.comments import SKIP_AUTHORS
    bot_ph = ",".join("?" * len(SKIP_AUTHORS))
    comment_rows = conn.execute(
        f"SELECT author, body, score, permalink FROM comments "
        f"WHERE sighting_id=? AND LOWER(TRIM(author)) NOT IN ({bot_ph}) "
        f"ORDER BY score DESC", (sighting_id, *sorted(SKIP_AUTHORS))
    ).fetchall()
    related = related_sightings(conn, row)
    related_map = None
    if related:
        related_map = (f"/map?from={row['sighted_at'][:10]}&to={row['sighted_at'][:10]}")
    # sky-context: hand analysts the exact time/place (and camera heading)
    sky = None
    if row["lat"] is not None:
        lat, lon, day = row["lat"], row["lon"], row["sighted_at"][:10]
        heading = None
        for m in media:
            meta = json.loads(m["exif_json"]) if m["exif_json"] else {}
            if meta.get("compass_deg") is not None:
                heading = {"deg": meta["compass_deg"],
                           "name": helpers.compass_name(meta["compass_deg"]),
                           "ref": meta.get("compass_ref", "true")}
                break
        hhmm = row["sighted_at"][11:16]
        sats = json.loads(row["sky_events"]) if row["sky_events"] else None
        sky = {
            "sats": sats,
            # tar1090 playback: ?replay=YYYY-MM-DD-HH:MM rewinds the whole
            # area to that moment (showTrace needs a specific airframe)
            "adsb": (f"https://globe.adsbexchange.com/?lat={lat:.3f}&lon={lon:.3f}"
                     f"&zoom=9&replay={day}-{hhmm}"),
            # FR24 parses >2 decimals as a flight callsign ("flight not found")
            "fr24": f"https://www.flightradar24.com/{lat:.2f},{lon:.2f}/9",
            "heavens": f"https://www.heavens-above.com/?lat={lat:.4f}&lng={lon:.4f}",
            # in-the-sky honors date params (location is a one-time setting
            # on their side); timeanddate ignored ?month/year — verified
            "skychart": (f"https://in-the-sky.org/skymap.php?year={day[:4]}"
                         f"&month={int(day[5:7])}&day={int(day[8:10])}"
                         f"&latitude={lat:.4f}&longitude={lon:.4f}"),
            "heading": heading,
        }
    base = get_settings().base_url
    return templates.TemplateResponse(
        request, "detail.html",
        {"user": user, "s": s, "media": media_items, "reddit_url": reddit_url, "admin": admin,
         "comments": comment_rows,
         "canonical": f"{base}/sighting/{s['id']}/{s['slug']}",
         "page_desc": helpers.page_description(s),
         "related": related, "related_map": related_map, "sky": sky,
         "csrf_token": auth.csrf_for(user.id) if user else ""},
    )


@router.get("/map")
def map_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse(
        request, "map.html",
        {"user": user, "shapes": helpers.SHAPES,
         "canonical": f"{get_settings().base_url}/map"}
    )


@router.get("/investigate")
def investigate(request: Request, user=Depends(current_user)):
    categories = sorted({c for e in INVESTIGATE_ENTRIES for c in e["categories"]})
    return templates.TemplateResponse(
        request, "investigate.html",
        {"user": user, "entries": INVESTIGATE_ENTRIES, "categories": categories,
         "canonical": f"{get_settings().base_url}/investigate"},
    )


@router.get("/api/pins")
def pins(
    shape: str = "",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    conn=Depends(db.get_db),
):
    """Compact [id, lat, lon, date] arrays — the map only needs coordinates
    up front; popup content comes from /api/pins/{id} on click. Straight SQL
    (no Meili hop, no card hydration, no cap): stays fast past 20K pins."""
    where = ["lat IS NOT NULL", "lon IS NOT NULL",
             f"status IN {PUBLIC_STATUSES_SQL}"]
    args: list = []
    if shape:
        # same mention-or-structured match as the gallery (see query_sightings)
        where.append("(shape = ? OR id IN "
                     "(SELECT rowid FROM sightings_fts WHERE sightings_fts MATCH ?))")
        args.extend([shape, f'"{shape}"*'])
    if date_from:
        where.append("substr(sighted_at, 1, 10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(sighted_at, 1, 10) <= ?")
        args.append(date_to)
    rows = conn.execute(
        f"SELECT id, lat, lon, substr(sighted_at, 1, 10) AS d "
        f"FROM sightings WHERE {' AND '.join(where)} ORDER BY id", args)
    # 4 decimals ≈ 11 m — plenty for a world map, noticeably smaller JSON
    return {"pins": [[r["id"], round(r["lat"], 4), round(r["lon"], 4), r["d"]]
                     for r in rows]}


@router.get("/api/pins/{sighting_id}")
def pin_detail(sighting_id: int, conn=Depends(db.get_db)):
    row = conn.execute(
        f"""SELECT id, title, sighted_at, shape,
              (SELECT thumb_key FROM media m WHERE m.sighting_id = s.id
                 AND m.thumb_key IS NOT NULL
               ORDER BY sort_order LIMIT 1) AS thumb_key
            FROM sightings s
            WHERE id = ? AND lat IS NOT NULL
              AND status IN {PUBLIC_STATUSES_SQL}""",
        (sighting_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such pin")
    return {
        "title": row["title"],
        "url": f"/sighting/{row['id']}/{helpers.slugify(row['title'])}",
        "thumb": r2.public_url(row["thumb_key"]) if row["thumb_key"] else None,
        "date": row["sighted_at"][:10],
        "shape": row["shape"],
    }


@router.get("/guide")
def guide(request: Request, user=Depends(current_user)):
    s = get_settings()
    return templates.TemplateResponse(
        request, "guide.html",
        {"user": user, "bot_username": s.script_username or "ufosightingsbot",
         "verify_hours": s.verify_window_hours,
         "canonical": f"{s.base_url}/guide"})


@router.get("/anonymous")
def anonymous(request: Request, user=Depends(current_user)):
    """How to submit UFO footage anonymously (Tor onion / GlobaLeaks). The
    onion is only shown, indexed, and sitemapped once ANONYMOUS_ENABLED is
    set — before the GlobaLeaks wizard is done, publishing it would let the
    first visitor claim admin of the fresh instance."""
    s = get_settings()
    return templates.TemplateResponse(
        request, "anonymous.html",
        {"user": user, "onion": s.anonymous_onion,
         "enabled": s.anonymous_enabled,
         "canonical": f"{s.base_url}/anonymous"})


@router.get("/search")
def search_redirect(request: Request):
    """Search lives on the gallery now — permanent redirect keeps old links."""
    qs = str(request.url.query)
    return Response(status_code=301,
                    headers={"Location": "/" + (f"?{qs}" if qs else "")})


@router.get("/sitemap.xml")
def sitemap(conn=Depends(db.get_db)):
    s = get_settings()
    base = s.base_url
    urls = [(f"{base}/", None), (f"{base}/map", None),
            (f"{base}/investigate", None), (f"{base}/guide", None)]
    if s.anonymous_enabled:
        urls.append((f"{base}/anonymous", None))
    for r in conn.execute(
        f"SELECT id, title, created_at FROM sightings "
        f"WHERE status IN {PUBLIC_STATUSES_SQL} ORDER BY id"
    ):
        urls.append((f"{base}/sighting/{r['id']}/{helpers.slugify(r['title'])}",
                     r["created_at"][:10]))
    def entry(u, lastmod):
        lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        return f"  <url><loc>{u}</loc>{lm}</url>"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entry(u, lm) for u, lm in urls)
        + "\n</urlset>"
    )
    return Response(content=body, media_type="application/xml")


@router.get("/feed.xml")
def feed(conn=Depends(db.get_db)):
    """RSS of the latest sightings — the UFO community lives in feed readers."""
    import email.utils
    from datetime import datetime, timezone
    base = get_settings().base_url
    rows = conn.execute(
        f"""SELECT id, title, description, created_at FROM sightings
            WHERE status IN {PUBLIC_STATUSES_SQL}
            ORDER BY created_at DESC LIMIT 50""").fetchall()
    items = []
    for r in rows:
        url = f"{base}/sighting/{r['id']}/{helpers.slugify(r['title'])}"
        dt = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        desc = (r["description"] or "")[:400]
        items.append(
            "<item>"
            f"<title>{_xml(r['title'])}</title>"
            f"<link>{url}</link>"
            f"<guid isPermaLink=\"true\">{url}</guid>"
            f"<pubDate>{email.utils.format_datetime(dt)}</pubDate>"
            f"<description>{_xml(desc)}</description>"
            "</item>")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>ufosighting.report — latest UFO sightings</title>"
        f"<link>{base}/</link>"
        "<description>New UFO sighting reports with original-quality media, "
        "from r/UFOs and direct submissions.</description>"
        + "".join(items) + "</channel></rss>")
    return Response(content=body, media_type="application/rss+xml")


def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


@router.get("/robots.txt")
def robots():
    base = get_settings().base_url
    return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")


@router.get("/{key_name}.txt")
def indexnow_key_file(key_name: str):
    """IndexNow verification file: /<key>.txt must return the key. Registered
    after /robots.txt so that literal route still wins; any other *.txt 404s."""
    key = get_settings().indexnow_key
    if key and key_name == key:
        return PlainTextResponse(key)
    raise HTTPException(status_code=404)
