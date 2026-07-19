"""Auto-post new public sightings to Bluesky (ufosighting.bsky.social).

Runs as a sweep at the end of ingest_once — decoupled from per-row DB writes so
we never hold the SQLite write lock across a network call. Posts a native image
embed (the sighting's R2 thumbnail) + title/location/date + a link back +
hashtags. Forward-only + deduped via sightings.bsky_posted_at. Best-effort:
every failure is logged and never breaks ingest.
"""
import time
from datetime import datetime, timezone

import httpx

from app import helpers, r2
from app.config import get_settings

BASE = "https://bsky.social/xrpc"
POST_LIMIT_DEFAULT = 8
MAX_TEXT = 300          # Bluesky's grapheme cap; len() is a safe approximation here
MAX_BLOB = 1_000_000    # Bluesky image blob ceiling (~1MB)
BASE_TAGS = ["UFO", "UAP", "UFOsighting"]
_COUNTRY = {"usa": "USA", "united states": "USA", "us": "USA", "u.s.a.": "USA",
            "uk": "UK", "united kingdom": "UK", "u.k.": "UK"}


def enabled() -> bool:
    s = get_settings()
    return bool(s.bsky_enabled and s.bsky_handle and s.bsky_app_password)


# ---- post construction (pure, unit-testable) -------------------------------

def _clean_tag(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def _hashtags(row) -> list[str]:
    tags = list(BASE_TAGS)
    shape = (row["shape"] or "").strip()
    if shape and shape.lower() != "unknown":
        t = _clean_tag(shape.title())
        if t and t not in tags:
            tags.append(t)
    country = (row["country"] or "").strip()
    if country:
        t = _COUNTRY.get(country.lower()) or _clean_tag(country)
        if t and t not in tags:
            tags.append(t)
    return tags


def build_post_text(row) -> tuple[str, str]:
    """Return (display_text, url). Truncates the title to stay within MAX_TEXT."""
    s = get_settings()
    slug = helpers.slugify(row["title"] or "sighting")
    url = f"{s.base_url}/sighting/{row['id']}/{slug}"
    display_url = url.split("://", 1)[-1]
    loc = (row["location_text"] or row["city"] or row["country"] or "").strip()
    date = (row["sighted_at"] or "")[:10]
    meta_bits = []
    if loc:
        meta_bits.append(f"\U0001F4CD {loc}")   # 📍
    if date:
        meta_bits.append(f"\U0001F4C5 {date}")   # 📅
    shape = (row["shape"] or "").strip()
    if shape and shape.lower() != "unknown":
        meta_bits.append(shape.title())
    meta = " · ".join(meta_bits)
    tagline = " ".join("#" + t for t in _hashtags(row))

    def assemble(title):
        parts = [title]
        if meta:
            parts.append(meta)
        parts.append(display_url)
        parts.append(tagline)
        return "\n".join(parts)

    title = (row["title"] or "UFO sighting").strip()
    text = assemble(title)
    while len(text) > MAX_TEXT and len(title) > 15:
        title = title[:-6].rstrip() + "…"
        text = assemble(title)
    return text, url


def _facets(text: str, url: str, tags: list[str]) -> list[dict]:
    """Byte-offset facets for the link + each hashtag. Left-to-right moving cursor
    so #UFO doesn't match inside #UFOsighting and emoji byte-widths are correct."""
    facets = []
    cursor = 0
    targets = [(url.split("://", 1)[-1],
                {"$type": "app.bsky.richtext.facet#link", "uri": url})]
    for t in tags:
        targets.append(("#" + t,
                        {"$type": "app.bsky.richtext.facet#tag", "tag": t}))
    for sub, feature in targets:
        i = text.find(sub, cursor)
        if i < 0:
            continue
        start = len(text[:i].encode("utf-8"))
        end = start + len(sub.encode("utf-8"))
        facets.append({"index": {"byteStart": start, "byteEnd": end},
                       "features": [feature]})
        cursor = i + len(sub)
    return facets


# ---- Bluesky API -----------------------------------------------------------

def create_session() -> dict:
    s = get_settings()
    r = httpx.post(f"{BASE}/com.atproto.server.createSession",
                   json={"identifier": s.bsky_handle, "password": s.bsky_app_password},
                   timeout=30)
    r.raise_for_status()
    d = r.json()
    return {"jwt": d["accessJwt"], "did": d["did"]}


def upload_blob(session: dict, data: bytes, mime: str) -> dict:
    r = httpx.post(f"{BASE}/com.atproto.repo.uploadBlob", content=data,
                   headers={"Authorization": f"Bearer {session['jwt']}",
                            "Content-Type": mime}, timeout=30)
    r.raise_for_status()
    return r.json()["blob"]


def _thumb_key(conn, sighting_id):
    row = conn.execute(
        "SELECT thumb_key FROM media WHERE sighting_id=? AND thumb_key IS NOT NULL "
        "AND thumb_key<>'' ORDER BY sort_order LIMIT 1", (sighting_id,)).fetchone()
    return row[0] if row else None


def _image_embed(conn, row, session):
    key = _thumb_key(conn, row["id"])
    if not key:
        return None
    try:
        data = httpx.get(r2.public_url(key), timeout=30).content
    except httpx.HTTPError:
        return None
    if not data or len(data) > MAX_BLOB:
        return None
    blob = upload_blob(session, data, "image/jpeg")
    return {"$type": "app.bsky.embed.images",
            "images": [{"alt": (row["title"] or "")[:300], "image": blob}]}


def post_sighting(conn, row, *, session=None) -> str | None:
    """Post one sighting. Returns the record uri. Raises on API failure."""
    session = session or create_session()
    text, url = build_post_text(row)
    record = {
        "$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    facets = _facets(text, url, _hashtags(row))
    if facets:
        record["facets"] = facets
    embed = _image_embed(conn, row, session)
    if embed:
        record["embed"] = embed
    r = httpx.post(f"{BASE}/com.atproto.repo.createRecord",
                   json={"repo": session["did"], "collection": "app.bsky.feed.post",
                         "record": record},
                   headers={"Authorization": f"Bearer {session['jwt']}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("uri")


ELIGIBLE_SQL = """
SELECT * FROM sightings s
WHERE s.bsky_posted_at IS NULL
  AND s.status = 'live'
  AND EXISTS (SELECT 1 FROM media m WHERE m.sighting_id = s.id)
  AND (s.lat IS NOT NULL OR length(coalesce(s.description, '')) >= 80)
ORDER BY s.id DESC
LIMIT ?
"""


def eligible_rows(conn, limit=POST_LIMIT_DEFAULT):
    return conn.execute(ELIGIBLE_SQL, (limit,)).fetchall()


def post_new(conn, limit=POST_LIMIT_DEFAULT) -> dict:
    """Sweep: post up to `limit` new eligible sightings. Best-effort."""
    if not enabled():
        return {"posted": 0, "disabled": True}
    rows = eligible_rows(conn, limit)
    if not rows:
        return {"posted": 0}
    try:
        session = create_session()
    except Exception as exc:  # noqa: BLE001
        print(f"bsky: session failed: {exc}")
        return {"posted": 0, "error": str(exc)[:120]}
    posted = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows:
        try:
            uri = post_sighting(conn, row, session=session)
            conn.execute("UPDATE sightings SET bsky_posted_at=? WHERE id=?",
                         (now, row["id"]))
            conn.commit()
            posted += 1
            print(f"bsky: posted sighting {row['id']} -> {uri}")
            time.sleep(1.5)
        except Exception as exc:  # noqa: BLE001 — leave NULL, retried next sweep
            print(f"bsky: post failed for sighting {row['id']}: {exc}")
    return {"posted": posted}
