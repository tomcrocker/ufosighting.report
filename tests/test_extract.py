from app import extract


def test_combine_labels_sources():
    post = {"title": "Orb over Tofino", "selftext": "Saw it at dusk."}
    text = extract.combine_post_text(post, ["It was near the pier", "About 9pm"])
    assert "Orb over Tofino" in text and "Saw it at dusk." in text
    assert "near the pier" in text and "About 9pm" in text
    assert "TITLE" in text and "OP COMMENT" in text


def test_combine_truncates():
    post = {"title": "t", "selftext": "x" * 20000}
    text = extract.combine_post_text(post, [])
    assert len(text) <= 6500  # capped


def test_clamp_keeps_valid():
    raw = {"date": "2026-07-01", "time": "22:15", "timezone": "America/Vancouver",
           "location_text": "Lake Cowichan, BC", "city": "Lake Cowichan", "country": "Canada",
           "shape": "Sphere", "num_objects": "2", "duration_seconds": 120, "summary": "An orb."}
    c = extract.validate_and_clamp(raw, post_created_iso="2026-07-05T00:00:00Z")
    assert c["date"] == "2026-07-01" and c["time"] == "22:15"
    assert c["timezone"] == "America/Vancouver"
    assert c["shape"] == "sphere" and c["num_objects"] == "2"
    assert c["duration_seconds"] == 120 and c["city"] == "Lake Cowichan"


def test_clamp_drops_future_and_ancient_dates():
    assert extract.validate_and_clamp({"date": "2999-01-01"}, post_created_iso="2026-07-05T00:00:00Z")["date"] is None
    assert extract.validate_and_clamp({"date": "1800-01-01"}, post_created_iso="2026-07-05T00:00:00Z")["date"] is None


def test_clamp_drops_bad_values():
    raw = {"time": "9pm", "timezone": "Mars/Olympus", "shape": "mothership",
           "num_objects": "lots", "duration_seconds": 999999}
    c = extract.validate_and_clamp(raw, post_created_iso="2026-07-05T00:00:00Z")
    assert c["time"] is None and c["timezone"] is None and c["shape"] is None
    assert c["num_objects"] is None and c["duration_seconds"] is None


def test_clamp_handles_empty():
    c = extract.validate_and_clamp({}, post_created_iso="2026-07-05T00:00:00Z")
    assert all(c[k] is None for k in ("date", "time", "location_text", "shape"))


import httpx  # noqa: E402
import respx  # noqa: E402
from app.config import get_settings  # noqa: E402

CHAT = "https://api.x.ai/v1/chat/completions"


def _chat_response(content: str):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_extract_fields_empty_key_returns_empty(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "")
    get_settings.cache_clear()
    assert extract.extract_fields("anything") == {}


@respx.mock
def test_extract_fields_parses_json(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    get_settings.cache_clear()
    route = respx.post(CHAT).mock(return_value=_chat_response(
        '{"date":"2026-07-01","location_text":"Tofino, BC","shape":"sphere"}'))
    out = extract.extract_fields("Orb over Tofino on 2026-07-01")
    assert out["date"] == "2026-07-01" and out["location_text"] == "Tofino, BC"
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer xai-test"


@respx.mock
def test_extract_fields_non_json_returns_empty(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    get_settings.cache_clear()
    respx.post(CHAT).mock(return_value=_chat_response("sorry, I cannot help"))
    assert extract.extract_fields("x") == {}


@respx.mock
def test_extract_fields_network_error_returns_empty(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    get_settings.cache_clear()
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("down"))
    assert extract.extract_fields("x") == {}
