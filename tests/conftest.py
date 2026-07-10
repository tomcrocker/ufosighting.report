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
    try:
        from app import r2
        r2.client.cache_clear()
    except ImportError:
        pass


@pytest.fixture
def db_conn(tmp_path):
    from app import db
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(start_thumb_worker=False)) as c:
        yield c


@pytest.fixture
def app_db(client):
    from app import db
    conn = db.connect(os.environ["DB_PATH"])
    yield conn
    conn.close()


@pytest.fixture
def logged_in(client, app_db):
    from app import auth
    sid = auth.create_session(app_db, "tester", "tok-abc", 3600)
    client.cookies.set("sid", sid)
    client.sid = sid
    return client
