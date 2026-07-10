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
