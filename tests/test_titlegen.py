import httpx
import respx

from app import titlegen
from app.config import get_settings

CLEAN = {"sighted_at": "2026-07-15T13:28:00Z", "tz_name": "America/New_York",
         "num_objects": "3", "shape": "sphere", "movement": ["hovering", "erratic"],
         "location_text": "St. Bernard, Ohio", "description": "Three round lights."}


def _llm(monkeypatch, content):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("AI_TITLES_ENABLED", "1")
    get_settings.cache_clear()


@respx.mock
def test_standardizes_and_appends_date(monkeypatch):
    _llm(monkeypatch, None)
    respx.post("https://llm.test/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": "Three silent round lights over St. Bernard, Ohio"}}]}))
    out = titlegen.generate("OMG ORBS?!?! you won't believe this", CLEAN)
    get_settings.cache_clear()
    assert out == "Three silent round lights over St. Bernard, Ohio — Jul 15, 2026, 9:28 AM"


@respx.mock
def test_sanitizes_model_noise(monkeypatch):
    _llm(monkeypatch, None)
    respx.post("https://llm.test/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": 'Title: "Capsule-shaped object over Rochester, NY"\nextra line'}}]}))
    out = titlegen.generate("x", CLEAN)
    get_settings.cache_clear()
    assert out.startswith("Capsule-shaped object over Rochester, NY —")
    assert '"' not in out and "\n" not in out and "Title:" not in out


@respx.mock
def test_falls_back_to_user_title_on_error(monkeypatch):
    _llm(monkeypatch, None)
    respx.post("https://llm.test/v1/chat/completions").mock(return_value=httpx.Response(500))
    out = titlegen.generate("My original title", CLEAN)
    get_settings.cache_clear()
    assert out == "My original title"


def test_disabled_returns_user_title(monkeypatch):
    monkeypatch.setenv("AI_TITLES_ENABLED", "0")
    monkeypatch.setenv("LLM_API_KEY", "k")
    get_settings.cache_clear()
    out = titlegen.generate("My original title", CLEAN)
    get_settings.cache_clear()
    assert out == "My original title"


def test_no_llm_key_returns_user_title(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    out = titlegen.generate("My original title", CLEAN)
    get_settings.cache_clear()
    assert out == "My original title"
