import urllib.parse

import httpx

from app.config import get_settings

AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
ME_URL = "https://oauth.reddit.com/api/v1/me"


class AuthError(Exception):
    pass


def login_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.reddit_client_id,
        "response_type": "code",
        "state": state,
        "redirect_uri": s.reddit_redirect_uri,
        "duration": "temporary",
        "scope": "identity submit",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> str:
    s = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": s.reddit_redirect_uri,
        },
        auth=(s.reddit_client_id, s.reddit_client_secret),
        headers={"User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200:
        raise AuthError(f"token exchange failed: HTTP {resp.status_code}")
    token = resp.json().get("access_token")
    if not token:
        raise AuthError(f"token exchange failed: {resp.json()}")
    return token


def fetch_username(access_token: str) -> str:
    s = get_settings()
    resp = httpx.get(
        ME_URL,
        headers={"Authorization": f"bearer {access_token}", "User-Agent": s.user_agent},
        timeout=15,
    )
    if resp.status_code != 200:
        raise AuthError(f"identity fetch failed: HTTP {resp.status_code}")
    return resp.json()["name"]
