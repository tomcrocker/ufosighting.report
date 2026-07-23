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
    read_username: str
    read_password: str
    subreddit: str
    sighting_flair_id: str
    admin_users: tuple[str, ...]
    admin_password: str
    user_agent: str
    session_ttl_seconds: int
    max_image_bytes: int
    max_video_bytes: int
    max_files: int
    turnstile_site_key: str
    turnstile_secret_key: str
    rate_submit_per_hour: int
    rate_submit_per_day: int
    rate_presign_per_hour: int
    rate_geocode_per_hour: int
    submit_per_username_hours: int
    verify_window_hours: int
    verify_dm_per_username_hours: int
    cqs_min_account_age_days: int
    cqs_min_karma: int
    cqs_min_link_karma: int
    cqs_min_comment_karma: int
    cqs_require_verified_email: bool
    ai_titles_enabled: bool
    account_intel_enabled: bool
    discord_webhook_url: str
    intel_dormancy_gap_days: int
    intel_reactivation_recent_days: int
    intel_min_karma_per_year: int
    xai_api_key: str
    xai_model: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_reasoning_off: bool
    ingest_subreddit: str
    meili_url: str
    meili_key: str
    meili_index: str
    indexnow_key: str
    anonymous_onion: str
    anonymous_enabled: bool
    ga_measurement_id: str
    dc_gag_enabled: bool
    bsky_enabled: bool
    bsky_handle: str
    bsky_app_password: str


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@lru_cache
def get_settings() -> Settings:
    subreddit = _env("SUBREDDIT")
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
        read_username=_env("READ_USERNAME", ""),
        read_password=_env("READ_PASSWORD", ""),
        subreddit=subreddit,
        sighting_flair_id=_env("SIGHTING_FLAIR_ID", ""),
        admin_users=tuple(
            u.strip().lower() for u in _env("ADMIN_USERS", "").split(",") if u.strip()
        ),
        admin_password=_env("ADMIN_PASSWORD", ""),
        user_agent="web:report.ufosighting:v1.0 (by /u/tmosh)",
        session_ttl_seconds=int(_env("SESSION_TTL_SECONDS", "2592000")),  # 30d default
        max_image_bytes=25 * 1024 * 1024,
        max_video_bytes=500 * 1024 * 1024,
        max_files=10,
        turnstile_site_key=_env("TURNSTILE_SITE_KEY", ""),
        turnstile_secret_key=_env("TURNSTILE_SECRET_KEY", ""),
        rate_submit_per_hour=int(_env("RATE_SUBMIT_PER_HOUR", "5")),
        rate_submit_per_day=int(_env("RATE_SUBMIT_PER_DAY", "8")),
        rate_presign_per_hour=int(_env("RATE_PRESIGN_PER_HOUR", "40")),
        rate_geocode_per_hour=int(_env("RATE_GEOCODE_PER_HOUR", "60")),
        # one sighting per Reddit account per day (they post as themselves, so
        # more than that from one account is spam)
        submit_per_username_hours=int(_env("SUBMIT_PER_USERNAME_HOURS", "24")),
        verify_window_hours=int(_env("VERIFY_WINDOW_HOURS", "6")),
        # CQS-proxy gate: accounts below these auto-route to the review queue.
        # Loose by default — target throwaways, not ordinary participants.
        cqs_min_account_age_days=int(_env("CQS_MIN_ACCOUNT_AGE_DAYS", "30")),
        cqs_min_karma=int(_env("CQS_MIN_KARMA", "50")),
        # per-type floors default to 0 = "must not be negative"; raise either to
        # demand a real track record in that kind of contribution
        cqs_min_link_karma=int(_env("CQS_MIN_LINK_KARMA", "0")),
        cqs_min_comment_karma=int(_env("CQS_MIN_COMMENT_KARMA", "0")),
        cqs_require_verified_email=_env("CQS_REQUIRE_VERIFIED_EMAIL", "1") == "1",
        # Aged-account deep dive: age passes CQS, so also look at activity. A
        # long silence then a recent burst is the reactivated-account tell.
        ai_titles_enabled=_env("AI_TITLES_ENABLED", "1") == "1",
        account_intel_enabled=_env("ACCOUNT_INTEL_ENABLED", "1") == "1",
        discord_webhook_url=_env("DISCORD_WEBHOOK_URL", ""),
        intel_dormancy_gap_days=int(_env("INTEL_DORMANCY_GAP_DAYS", "180")),
        intel_reactivation_recent_days=int(_env("INTEL_REACTIVATION_RECENT_DAYS", "45")),
        intel_min_karma_per_year=int(_env("INTEL_MIN_KARMA_PER_YEAR", "15")),
        verify_dm_per_username_hours=int(_env("VERIFY_DM_PER_USERNAME_HOURS", "1")),
        xai_api_key=_env("XAI_API_KEY", ""),
        xai_model=_env("XAI_MODEL", "grok-3-mini"),
        llm_base_url=_env("LLM_BASE_URL", "https://api.x.ai/v1"),
        llm_api_key=_env("LLM_API_KEY", "") or _env("XAI_API_KEY", ""),
        llm_model=_env("LLM_MODEL", "") or _env("XAI_MODEL", "grok-3-mini"),
        llm_reasoning_off=_env("LLM_REASONING_OFF", "").strip().lower()
        in ("1", "true", "yes", "on"),
        ingest_subreddit=_env("INGEST_SUBREDDIT", "") or subreddit,
        meili_url=_env("MEILI_URL", ""),
        meili_key=_env("MEILI_KEY", ""),
        meili_index=_env("MEILI_INDEX", "sightings"),
        indexnow_key=_env("INDEXNOW_KEY", ""),
        anonymous_onion=_env(
            "ANONYMOUS_ONION",
            "4hqzw2mhq33gihjrwm6nldl2rhlqsgzurypttqwmtdoegw6cfvqd3lid.onion"),
        # off until the GlobaLeaks wizard is done — publishing the onion before
        # then lets the first visitor claim admin of the fresh instance
        anonymous_enabled=_env("ANONYMOUS_ENABLED", "").strip().lower()
        in ("1", "true", "yes", "on"),
        ga_measurement_id=_env("GA_MEASUREMENT_ID", "").strip(),
        # gag interstitial for Washington-DC visitors; needs Cloudflare's
        # "Add visitor location headers" managed transform (CF-Region-Code)
        dc_gag_enabled=_env("DC_GAG_ENABLED", "").strip().lower()
        in ("1", "true", "yes", "on"),
        bsky_enabled=_env("BSKY_ENABLED", "").strip().lower()
        in ("1", "true", "yes", "on"),
        bsky_handle=_env("BSKY_HANDLE", "").strip(),
        bsky_app_password=_env("BSKY_APP_PASSWORD", "").strip(),
    )
