"""Meilisearch client — typo-tolerant search + facets with SQLite fallback.

MEILI_URL empty ⇒ everything here is a silent no-op and callers use their SQL
paths. All write hooks are best-effort: a Meili failure never breaks a
submit/approve/ingest/sync.
"""
from datetime import datetime, timezone

import httpx

from app.config import get_settings

ISO = "%Y-%m-%dT%H:%M:%SZ"
PUBLIC_STATUSES = ("live", "deleted_by_user", "removed_on_reddit")
SYNONYMS = {
    "ufo": ["uap", "uaps"], "uap": ["ufo", "ufos"],
    "disc": ["disk", "saucer"], "disk": ["disc", "saucer"], "saucer": ["disc", "disk"],
    "tic-tac": ["tictac"], "tictac": ["tic-tac"],
    "orb": ["sphere"], "sphere": ["orb"],
}
SETTINGS = {
    "searchableAttributes": ["title", "description", "location_text", "city",
                             "country", "reddit_username"],
    "filterableAttributes": ["shape", "country", "source", "status", "media_kind",
                             "sighted_ts", "has_geo"],
    "sortableAttributes": ["sighted_ts", "reddit_score"],
    "synonyms": SYNONYMS,
}


def enabled() -> bool:
    return bool(get_settings().meili_url)


def _base():
    s = get_settings()
    return s.meili_url.rstrip("/"), {"Authorization": f"Bearer {s.meili_key}"}, s.meili_index


def build_doc(row, media_kind) -> dict:
    try:
        ts = int(datetime.strptime(row["sighted_at"], ISO)
                 .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        ts = 0
    return {
        "id": row["id"], "title": row["title"], "description": row["description"],
        "location_text": row["location_text"], "city": row["city"],
        "country": row["country"], "reddit_username": row["reddit_username"],
        "shape": row["shape"], "source": row["source"], "status": row["status"],
        "media_kind": media_kind, "sighted_ts": ts,
        "reddit_score": row["reddit_score"],
        "has_geo": row["lat"] is not None and row["lon"] is not None,
    }


def index_sightings(conn, ids) -> None:
    if not enabled() or not ids:
        return
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM sightings WHERE id IN ({marks})", list(ids)).fetchall()
        docs, dead = [], []
        for row in rows:
            if row["status"] in PUBLIC_STATUSES:
                mk = conn.execute(
                    "SELECT kind FROM media WHERE sighting_id=? ORDER BY sort_order LIMIT 1",
                    (row["id"],)).fetchone()
                docs.append(build_doc(row, mk["kind"] if mk else None))
            else:
                dead.append(row["id"])
        url, headers, index = _base()
        if docs:
            httpx.post(f"{url}/indexes/{index}/documents", headers=headers,
                       json=docs, timeout=10)
        if dead:
            httpx.post(f"{url}/indexes/{index}/documents/delete-batch",
                       headers=headers, json=dead, timeout=10)
    except httpx.HTTPError as exc:
        print(f"meili index failed: {exc}")


def delete_sightings(ids) -> None:
    if not enabled() or not ids:
        return
    try:
        url, headers, index = _base()
        httpx.post(f"{url}/indexes/{index}/documents/delete-batch",
                   headers=headers, json=list(ids), timeout=10)
    except httpx.HTTPError as exc:
        print(f"meili delete failed: {exc}")


def apply_settings() -> None:
    if not enabled():
        return
    url, headers, index = _base()
    httpx.put(f"{url}/indexes", headers=headers,
              json={"uid": index, "primaryKey": "id"}, timeout=10)
    httpx.patch(f"{url}/indexes/{index}/settings", headers=headers,
                json=SETTINGS, timeout=30)
