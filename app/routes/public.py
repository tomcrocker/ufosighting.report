import json
import math
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app import auth, db, helpers, r2
from app.config import get_settings
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
        where.append("s.shape = ?")
        args.append(shape)
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
    else:
        order = "s.featured DESC, s.sighted_at DESC"
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


@router.get("/")
def index(
    request: Request,
    shape: str = "",
    country: str = "",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    media: str = "",
    sort: str = "new",
    t: str = "all",
    page: int = 1,
    conn=Depends(db.get_db),
    user=Depends(current_user),
):
    page = max(1, page)
    if sort not in SORTS:
        sort = "new"
    if t not in TOP_WINDOW_HOURS:
        t = "all"
    rows, total = query_sightings(
        conn, shape=shape or None, country=country or None,
        date_from=date_from or None, date_to=date_to or None,
        media_kind=media or None, sort=sort, top_window=t, page=page,
    )
    countries = [
        r["country"] for r in conn.execute(
            f"""SELECT DISTINCT country FROM sightings
               WHERE status IN {PUBLIC_STATUSES_SQL} AND country IS NOT NULL AND country != ''
               ORDER BY country"""
        )
    ]
    filters = {"shape": shape, "country": country, "from": date_from, "to": date_to,
               "media": media}
    if sort != "new":
        filters["sort"] = sort
    if sort == "top" and t != "all":
        filters["t"] = t
    qs = urllib.parse.urlencode({k: v for k, v in filters.items() if v})
    return templates.TemplateResponse(
        request, "index.html",
        {
            "user": user,
            "cards": [card(r) for r in rows],
            "f": filters,
            "sort": sort,
            "t": t,
            "top_windows": list(TOP_WINDOW_HOURS),
            "countries": countries,
            "shapes": helpers.SHAPES,
            "page": page,
            "pages": max(1, math.ceil(total / PER_PAGE)),
            "total": total,
            "qs": qs,
        },
    )


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
    if row is None or (row["status"] not in PUBLIC_STATUSES and not admin):
        raise HTTPException(status_code=404)
    media = conn.execute(
        "SELECT * FROM media WHERE sighting_id=? ORDER BY sort_order", (sighting_id,)
    ).fetchall()
    s = dict(row)
    s["slug"] = helpers.slugify(row["title"])
    s["sighted_local"] = helpers.from_utc(row["sighted_at"], row["tz_name"])
    for field in ("movement", "sensors", "witness_background"):
        s[field] = json.loads(row[field]) if row[field] else []
    media_items = [
        {
            "url": r2.public_url(m["r2_key"]),
            "thumb_url": r2.public_url(m["thumb_key"]) if m["thumb_key"] else None,
            "kind": m["kind"],
        }
        for m in media
    ]
    reddit_url = None
    if row["reddit_post_id"]:
        # subreddit-agnostic permalink: reddit redirects /comments/{id}/ to the
        # canonical URL, correct for both bot posts (SUBREDDIT) and ingested
        # posts (INGEST_SUBREDDIT)
        reddit_url = f"https://www.reddit.com/comments/{row['reddit_post_id']}/"
    return templates.TemplateResponse(
        request, "detail.html",
        {"user": user, "s": s, "media": media_items, "reddit_url": reddit_url, "admin": admin,
         "csrf_token": auth.csrf_for(user.id) if user else ""},
    )


@router.get("/map")
def map_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse(
        request, "map.html", {"user": user, "shapes": helpers.SHAPES}
    )


@router.get("/api/pins")
def pins(
    shape: str = "",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    conn=Depends(db.get_db),
):
    rows, _ = query_sightings(
        conn, shape=shape or None,
        date_from=date_from or None, date_to=date_to or None,
        page=1, per_page=5000,
    )
    return {
        "pins": [
            {
                "id": r["id"],
                "title": r["title"],
                "lat": r["lat"],
                "lon": r["lon"],
                "url": f"/sighting/{r['id']}/{helpers.slugify(r['title'])}",
                "thumb": r2.public_url(r["thumb_key"]) if r["thumb_key"] else None,
                "date": r["sighted_at"][:10],
                "shape": r["shape"],
            }
            for r in rows
            if r["lat"] is not None and r["lon"] is not None
        ]
    }


@router.get("/search")
def search(request: Request, q: str = "", conn=Depends(db.get_db), user=Depends(current_user)):
    results = []
    query = q.strip()
    if query:
        match = " ".join('"' + term.replace('"', "") + '"' for term in query.split())
        rows = conn.execute(
            """SELECT s.*,
                  (SELECT m.thumb_key FROM media m WHERE m.sighting_id = s.id
                     ORDER BY m.sort_order LIMIT 1) AS thumb_key,
                  (SELECT m.kind FROM media m WHERE m.sighting_id = s.id
                     ORDER BY m.sort_order LIMIT 1) AS first_kind
               FROM sightings_fts f
               JOIN sightings s ON s.id = f.rowid
               WHERE sightings_fts MATCH ? AND s.status IN ('live', 'deleted_by_user', 'removed_on_reddit')
               ORDER BY f.rank LIMIT 60""",
            (match,),
        ).fetchall()
        results = [card(r) for r in rows]
    return templates.TemplateResponse(
        request, "search.html", {"user": user, "q": q, "cards": results}
    )


@router.get("/sitemap.xml")
def sitemap(conn=Depends(db.get_db)):
    base = get_settings().base_url
    urls = [f"{base}/", f"{base}/map", f"{base}/search"]
    for r in conn.execute(
        f"SELECT id, title FROM sightings WHERE status IN {PUBLIC_STATUSES_SQL} ORDER BY id"
    ):
        urls.append(f"{base}/sighting/{r['id']}/{helpers.slugify(r['title'])}")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
        + "\n</urlset>"
    )
    return Response(content=body, media_type="application/xml")


@router.get("/robots.txt")
def robots():
    base = get_settings().base_url
    return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")
