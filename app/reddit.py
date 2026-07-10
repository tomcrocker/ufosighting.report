import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SUBMIT_URL = "https://oauth.reddit.com/api/submit"
INFO_URL = "https://oauth.reddit.com/api/info"


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


_script_token: dict = {"token": None, "expires": 0.0}


def script_token() -> str:
    if _script_token["token"] and time.time() < _script_token["expires"] - 60:
        return _script_token["token"]
    s = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={"grant_type": "password", "username": s.script_username, "password": s.script_password},
        auth=(s.script_client_id, s.script_client_secret),
        headers={"User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200 or "access_token" not in resp.json():
        raise RedditError(f"script token failed: HTTP {resp.status_code}")
    data = resp.json()
    _script_token["token"] = data["access_token"]
    _script_token["expires"] = time.time() + float(data.get("expires_in", 3600))
    return _script_token["token"]


@dataclass
class PostInfo:
    removed_by_category: str | None
    score: int
    num_comments: int


def fetch_posts_info(post_ids: list[str]) -> dict[str, PostInfo]:
    out: dict[str, PostInfo] = {}
    if not post_ids:
        return out
    token = script_token()
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
            )
    return out


def status_from_removed_by_category(rbc: str | None) -> str:
    if rbc is None:
        return "live"
    if rbc == "deleted":
        return "deleted_by_user"
    return "removed_on_reddit"
