import pytest
from app.config import get_settings


def test_settings_load_from_env():
    s = get_settings()
    assert s.subreddit == "UFOs_sandbox"
    assert s.max_files == 10
    assert s.max_image_bytes == 25 * 1024 * 1024
    assert s.max_video_bytes == 500 * 1024 * 1024
    assert s.user_agent == "web:report.ufosighting:v1.0 (by /u/tmosh)"


def test_admin_users_parsed_lowercase():
    s = get_settings()
    assert s.admin_users == ("tmosh", "adminuser")


def test_missing_required_env_raises(monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        get_settings()


def test_extraction_settings_defaults(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)
    monkeypatch.delenv("INGEST_SUBREDDIT", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.xai_api_key == ""
    assert s.xai_model == "grok-3-mini"
    assert s.ingest_subreddit == s.subreddit   # falls back to SUBREDDIT


def test_ingest_subreddit_override(monkeypatch):
    monkeypatch.setenv("INGEST_SUBREDDIT", "UFOs")
    get_settings.cache_clear()
    assert get_settings().ingest_subreddit == "UFOs"


def test_new_settings_defaults():
    s = get_settings()
    assert s.rate_submit_per_hour == 5
    assert s.rate_presign_per_hour == 40
    assert s.rate_geocode_per_hour == 60
    assert s.verify_window_hours == 6
    assert s.verify_dm_per_username_hours == 1
    assert s.turnstile_site_key == ""       # unset in test env
    assert s.turnstile_secret_key == ""
