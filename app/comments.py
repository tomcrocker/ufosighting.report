"""Top Reddit comments per sighting: fetch + wholesale-replace storage.
Best-effort everywhere — a failed fetch never raises out of sync and never
clobbers previously stored comments (archive philosophy)."""
import httpx

from app.config import get_settings

TOP_N = 10
# Automated accounts whose comments are noise on a sighting page. Stored
# lowercased; match with is_skipped_author (Reddit usernames are
# case-insensitive, so compare case-insensitively).
SKIP_AUTHORS = {"automoderator", "collapsebot", "ufomodbot"}
SKIP_BODIES = {"", "[deleted]", "[removed]"}


def is_skipped_author(author: str | None) -> bool:
    return (author or "").strip().lower() in SKIP_AUTHORS


def fetch_top_comments(token: str, post_id: str, *, limit: int = 50) -> list[dict]:
    s = get_settings()
    try:
        resp = httpx.get(
            f"https://oauth.reddit.com/comments/{post_id}",
            params={"sort": "top", "depth": 1, "limit": limit},
            headers={"Authorization": f"bearer {token}", "User-Agent": s.user_agent},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        listing = resp.json()
        if len(listing) < 2:
            return []
        out = []
        script_user = (s.script_username or "").strip().lower()
        for child in listing[1]["data"]["children"]:
            if child.get("kind") != "t1":
                continue
            d = child.get("data", {})
            author = d.get("author")
            if (is_skipped_author(author) or (author or "").strip().lower() == script_user
                    or (d.get("body") or "").strip() in SKIP_BODIES):
                continue
            out.append({"id": d.get("id"), "author": d.get("author"),
                        "body": d.get("body"), "score": int(d.get("score") or 0),
                        "created_utc": int(d.get("created_utc") or 0),
                        "permalink": d.get("permalink") or ""})
        return out
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []


def refresh_for_sighting(conn, token: str, sighting_id: int, reddit_post_id: str) -> int:
    fetched = fetch_top_comments(token, reddit_post_id)
    if not fetched:
        return 0  # keep whatever we had — fetch failures must not erase the archive
    top = sorted(fetched, key=lambda c: c["score"], reverse=True)[:TOP_N]
    conn.execute("DELETE FROM comments WHERE sighting_id=?", (sighting_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO comments (reddit_comment_id, sighting_id, author, body,"
        " score, created_utc, permalink) VALUES (?,?,?,?,?,?,?)",
        [(c["id"], sighting_id, c["author"], c["body"], c["score"],
          c["created_utc"], c["permalink"]) for c in top])
    conn.commit()
    return len(top)
