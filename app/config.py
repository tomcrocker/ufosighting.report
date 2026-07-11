import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    base_url: str
    db_path: str
    secret_key: str
    media_base_url: str
    r2_endpoint: str
    r2_bucket: str
    r2_access_key: str
    r2_secret_key: str
    reddit_client_id: str
    reddit_client_secret: str
    reddit_redirect_uri: str
    script_client_id: str
    script_client_secret: str
    script_username: str
    script_password: str
    subreddit: str
    sighting_flair_id: str
    admin_users: tuple[str, ...]
    user_agent: str
    session_ttl_seconds: int
    max_image_bytes: int
    max_video_bytes: int
    max_files: int
    turnstile_site_key: str
    turnstile_secret_key: str
    rate_submit_per_hour: int
    rate_presign_per_hour: int
    rate_geocode_per_hour: int
    verify_window_hours: int
    verify_dm_per_username_hours: int


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@lru_cache
def get_settings() -> Settings:
    return Settings(
        base_url=_env("BASE_URL", "http://localhost:8010").rstrip("/"),
        db_path=_env("DB_PATH", "data/sightings.db"),
        secret_key=_env("SECRET_KEY"),
        media_base_url=_env("MEDIA_BASE_URL").rstrip("/"),
        r2_endpoint=_env("R2_ENDPOINT"),
        r2_bucket=_env("R2_BUCKET"),
        r2_access_key=_env("R2_ACCESS_KEY"),
        r2_secret_key=_env("R2_SECRET_KEY"),
        reddit_client_id=_env("REDDIT_CLIENT_ID"),
        reddit_client_secret=_env("REDDIT_CLIENT_SECRET"),
        reddit_redirect_uri=_env("REDDIT_REDIRECT_URI"),
        script_client_id=_env("SCRIPT_CLIENT_ID", ""),
        script_client_secret=_env("SCRIPT_CLIENT_SECRET", ""),
        script_username=_env("SCRIPT_USERNAME", ""),
        script_password=_env("SCRIPT_PASSWORD", ""),
        subreddit=_env("SUBREDDIT"),
        sighting_flair_id=_env("SIGHTING_FLAIR_ID", ""),
        admin_users=tuple(
            u.strip().lower() for u in _env("ADMIN_USERS", "").split(",") if u.strip()
        ),
        user_agent="web:report.ufosighting:v1.0 (by /u/tmosh)",
        session_ttl_seconds=3600,
        max_image_bytes=25 * 1024 * 1024,
        max_video_bytes=500 * 1024 * 1024,
        max_files=10,
        turnstile_site_key=_env("TURNSTILE_SITE_KEY", ""),
        turnstile_secret_key=_env("TURNSTILE_SECRET_KEY", ""),
        rate_submit_per_hour=int(_env("RATE_SUBMIT_PER_HOUR", "5")),
        rate_presign_per_hour=int(_env("RATE_PRESIGN_PER_HOUR", "40")),
        rate_geocode_per_hour=int(_env("RATE_GEOCODE_PER_HOUR", "60")),
        verify_window_hours=int(_env("VERIFY_WINDOW_HOURS", "6")),
        verify_dm_per_username_hours=int(_env("VERIFY_DM_PER_USERNAME_HOURS", "1")),
    )
