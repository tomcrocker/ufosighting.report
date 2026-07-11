import httpx
import respx

from app import turnstile
from app.config import get_settings

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def test_dev_bypass_when_no_secret():
    assert turnstile.verify("anything") is True  # test env has empty secret


@respx.mock
def test_success(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "sekret")
    get_settings.cache_clear()
    respx.post(SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    assert turnstile.verify("good-token", "1.2.3.4") is True


@respx.mock
def test_failure(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "sekret")
    get_settings.cache_clear()
    respx.post(SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": False}))
    assert turnstile.verify("bad-token") is False


@respx.mock
def test_network_error_is_false(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "sekret")
    get_settings.cache_clear()
    respx.post(SITEVERIFY).mock(side_effect=httpx.ConnectError("down"))
    assert turnstile.verify("tok") is False
