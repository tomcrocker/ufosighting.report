import os

TEST_ENV = {
    "BASE_URL": "http://testserver",
    "DB_PATH": "unused-set-per-test.db",
    "SECRET_KEY": "test-secret",
    "MEDIA_BASE_URL": "https://media.test",
    "R2_ENDPOINT": "https://r2.test",
    "R2_BUCKET": "test-bucket",
    "R2_ACCESS_KEY": "AKTEST",
    "R2_SECRET_KEY": "SKTEST",
    "REDDIT_CLIENT_ID": "webapp-id",
    "REDDIT_CLIENT_SECRET": "webapp-secret",
    "REDDIT_REDIRECT_URI": "http://testserver/auth/callback",
    "SCRIPT_CLIENT_ID": "script-id",
    "SCRIPT_CLIENT_SECRET": "script-secret",
    "SCRIPT_USERNAME": "modbot",
    "SCRIPT_PASSWORD": "hunter2",
    "SUBREDDIT": "UFOs_sandbox",
    "SIGHTING_FLAIR_ID": "flair-123",
    "ADMIN_USERS": "tmosh,AdminUser",
}
os.environ.update(TEST_ENV)

import pytest
from app.config import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
