import httpx

from app.config import get_settings

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify(token: str, remote_ip: str | None = None) -> bool:
    secret = get_settings().turnstile_secret_key
    if not secret:
        return True  # dev bypass when unconfigured
    data = {"secret": secret, "response": token or ""}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        resp = httpx.post(SITEVERIFY, data=data, timeout=10)
        return bool(resp.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False
