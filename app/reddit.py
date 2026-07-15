import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SUBMIT_URL = "https://oauth.reddit.com/api/submit"
INFO_URL = "https://oauth.reddit.com/api/info"
COMPOSE_URL = "https://oauth.reddit.com/api/compose"


class RedditError(Exception):
    pass


class TokenExpired(RedditError):
    pass


class RateLimited(RedditError):
    pass


def _headers(token: str) -> dict:
    return {"Authorization": f"bearer {token}", "User-Agent": get_settings().user_agent}


def submit_post(
    access_token: str, *, subreddit: str, title: str, body: str, flair_id: str = ""
) -> str:
    data = {
        "sr": subreddit,
        "kind": "self",
        "title": title[:300],
        "text": body,
        "api_type": "json",
        "sendreplies": "true",
        "resubmit": "true",
    }
    if flair_id:
        data["flair_id"] = flair_id
    resp = httpx.post(SUBMIT_URL, data=data, headers=_headers(access_token), timeout=30)
    if resp.status_code == 401:
        raise TokenExpired("Reddit session expired")
    if resp.status_code != 200:
        raise RedditError(f"reddit submit failed: HTTP {resp.status_code}")
    j = resp.json().get("json", {})
    errors = j.get("errors") or []
    if errors:
        code = errors[0][0]
        msg = errors[0][1] if len(errors[0]) > 1 else code
        if code == "RATELIMIT":
            raise RateLimited(msg)
        raise RedditError(f"{code}: {msg}")
    name = (j.get("data") or {}).get("name", "")
    if not name.startswith("t3_"):
        raise RedditError(f"unexpected submit response: {j}")
    return name.removeprefix("t3_")


_token_cache: dict[str, dict] = {}  # per-account token cache, keyed by username


def _password_token(username: str, password: str) -> str:
    cached = _token_cache.get(username)
    if cached and time.time() < cached["expires"] - 60:
        return cached["token"]
    s = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={"grant_type": "password", "username": username, "password": password},
        auth=(s.script_client_id, s.script_client_secret),
        headers={"User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200 or "access_token" not in resp.json():
        raise RedditError(f"token for u/{username} failed: HTTP {resp.status_code}")
    data = resp.json()
    _token_cache[username] = {"token": data["access_token"],
                              "expires": time.time() + float(data.get("expires_in", 3600))}
    return _token_cache[username]["token"]


def script_token() -> str:
    """Token for the BOT account — writes only (posts, comments, DMs), where
    the identity is what matters."""
    s = get_settings()
    return _password_token(s.script_username, s.script_password)


def read_token() -> str:
    """Token for public reads (listings, post info, comments). Uses the
    durable personal account when READ_USERNAME is set, so a spam-flag on the
    young bot account never takes down ingest/sync. Falls back to the bot."""
    s = get_settings()
    if s.read_username:
        return _password_token(s.read_username, s.read_password)
    return script_token()


@dataclass
class PostInfo:
    removed_by_category: str | None
    score: int
    num_comments: int
    created_utc: int | None = None  # default keeps positional callers working


def fetch_posts_info(post_ids: list[str]) -> dict[str, PostInfo]:
    out: dict[str, PostInfo] = {}
    if not post_ids:
        return out
    token = read_token()
    for i in range(0, len(post_ids), 100):
        chunk = post_ids[i : i + 100]
        resp = httpx.get(
            INFO_URL,
            params={"id": ",".join("t3_" + pid for pid in chunk)},
            headers=_headers(token),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RedditError(f"info fetch failed: HTTP {resp.status_code}")
        for child in resp.json()["data"]["children"]:
            d = child["data"]
            out[d["id"]] = PostInfo(
                removed_by_category=d.get("removed_by_category"),
                score=int(d.get("score", 0)),
                num_comments=int(d.get("num_comments", 0)),
                created_utc=int(d["created_utc"]) if d.get("created_utc") else None,
            )
    return out


def status_from_removed_by_category(rbc: str | None) -> str:
    if rbc is None:
        return "live"
    if rbc == "deleted":
        return "deleted_by_user"
    return "removed_on_reddit"


def approve(access_token: str, *, post_id: str | None = None,
            comment_id: str | None = None) -> None:
    """Mod-approve a post or comment (self-rescue for bot content the
    sitewide spam filter removes — works only where the account moderates)."""
    fullname = f"t3_{post_id}" if post_id else f"t1_{comment_id}"
    resp = httpx.post(
        "https://oauth.reddit.com/api/approve",
        data={"id": fullname},
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code != 200:
        raise RedditError(f"approve failed: HTTP {resp.status_code}")


def fetch_removed_bot_comments(access_token: str, post_id: str,
                               bot_username: str) -> list[str]:
    """Comment ids by the bot on this post that a mod-view shows as removed
    (spam-filtered). Requires a moderator token — removed comments are
    invisible in the public view."""
    try:
        resp = httpx.get(
            f"https://oauth.reddit.com/comments/{post_id}",
            params={"depth": 1, "limit": 20},
            headers=_headers(access_token),
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        listing = resp.json()
        if len(listing) < 2:
            return []
        out = []
        for child in listing[1]["data"]["children"]:
            d = child.get("data", {})
            if (d.get("author") == bot_username
                    and (d.get("banned_by") or d.get("removed") or d.get("spam"))):
                out.append(d["id"])
        return out
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []


def send_message(access_token: str, *, to: str, subject: str, text: str) -> None:
    resp = httpx.post(
        COMPOSE_URL,
        data={"api_type": "json", "to": to, "subject": subject[:100], "text": text},
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code == 401:
        raise TokenExpired("bot session expired")
    if resp.status_code != 200:
        raise RedditError(f"compose failed: HTTP {resp.status_code}")
    errors = (resp.json().get("json", {}) or {}).get("errors") or []
    if errors:
        code = errors[0][0]
        msg = errors[0][1] if len(errors[0]) > 1 else code
        if code == "RATELIMIT":
            raise RateLimited(msg)
        raise RedditError(f"{code}: {msg}")


def list_flair_posts(access_token, *, subreddit, flair, limit=100, after=None):
    params = {
        "q": f'flair_name:"{flair}"',
        "restrict_sr": 1,
        "sort": "new",
        "limit": limit,
        "type": "link",
    }
    if after:
        params["after"] = after
    resp = httpx.get(
        f"https://oauth.reddit.com/r/{subreddit}/search",
        params=params,
        headers=_headers(access_token),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RedditError(f"listing failed: HTTP {resp.status_code}")
    data = resp.json().get("data", {})
    return [c["data"] for c in data.get("children", [])], data.get("after")


def list_new_flair_posts(access_token, *, subreddit, flair, limit=100):
    """Newest posts of a flair, taken from the real-time /new listing and
    filtered client-side. Reddit's /search index lags minutes to hours behind
    for fresh posts (and sometimes never indexes them), so the live ingest
    must not trust search. /new is authoritative and immediate."""
    resp = httpx.get(
        f"https://oauth.reddit.com/r/{subreddit}/new",
        params={"limit": limit},
        headers=_headers(access_token),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RedditError(f"new listing failed: HTTP {resp.status_code}")
    want = flair.strip().lower()
    posts = [c["data"] for c in resp.json().get("data", {}).get("children", [])]
    return [p for p in posts
            if (p.get("link_flair_text") or "").strip().lower() == want]


def fetch_post(access_token, post_id):
    resp = httpx.get(
        INFO_URL,
        params={"id": "t3_" + post_id},
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code != 200:
        return None
    children = resp.json().get("data", {}).get("children", [])
    return children[0]["data"] if children else None
