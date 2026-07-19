"""Server-side visitor counting — adblock-proof (every request hits the origin,
no client JS to block, unlike GA). Privacy-first: a visitor is a daily-salted
hash of their real IP (CF-Connecting-IP), so raw IPs are never stored and the
same person can't be correlated across days. Accurate metric = DAILY uniques.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from app import db
from app.config import get_settings

# obvious non-humans: crawlers, libraries, scanners, uptime monitors
_BOT = re.compile(
    r"bot\b|crawl|spider|slurp|bingpreview|facebookexternalhit|embedly|headless|"
    r"python-|curl/|wget|libwww|okhttp|go-http|axios|node-fetch|monitor|uptime|"
    r"scan|semrush|ahrefs|dataprovider|censys|zgrab|masscan|feedfetcher|preview",
    re.I)
_SKIP_EXT = (".xml", ".txt", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp",
             ".json", ".js", ".css", ".webmanifest", ".map")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def client_ip(request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "0.0.0.0"))


def visitor_hash(ip: str, day: str) -> str:
    # daily salt (day in the digest) => dedups within a day, not reversible to
    # an IP, and not correlatable across days
    return hashlib.sha256(
        f"{ip}|{day}|{get_settings().secret_key}".encode()).hexdigest()[:16]


def is_countable(request, status: int) -> bool:
    """A real human page view: successful GET of a content page, non-bot UA."""
    if request.method != "GET" or status >= 400:
        return False
    p = request.url.path
    if p.startswith(("/static/", "/api/", "/dc/", "/auth/", "/admin", "/verify")):
        return False
    if p in ("/feed.xml", "/robots.txt", "/health", "/favicon.ico") or p.endswith(_SKIP_EXT):
        return False
    ua = request.headers.get("user-agent", "")
    return bool(ua) and not _BOT.search(ua)


def record(request, status: int) -> None:
    """Best-effort — analytics must never break serving a page."""
    if not is_countable(request, status):
        return
    day = _today()
    visitor = visitor_hash(client_ip(request), day)
    try:
        conn = db.connect(get_settings().db_path)
        try:
            # Best-effort: analytics runs in the response path, so cap the wait on a
            # busy DB at 1s (overriding the default 30s) and drop the count on lock
            # rather than stalling the page. A missed visit hit is fine; a 30s page
            # load is not.
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute(
                "INSERT INTO analytics_visits (day, visitor, hits) VALUES (?,?,1) "
                "ON CONFLICT(day, visitor) DO UPDATE SET hits = hits + 1",
                (day, visitor))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"analytics record failed: {exc}")


def summary(conn, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    daily = conn.execute(
        "SELECT day, COUNT(*) AS visitors, COALESCE(SUM(hits),0) AS views "
        "FROM analytics_visits WHERE day >= ? GROUP BY day ORDER BY day DESC",
        (since,)).fetchall()
    today = daily[0]["visitors"] if daily and daily[0]["day"] == _today() else 0
    n = len(daily)
    return {
        "daily": daily,
        "today": today,
        "avg7": round(sum(r["visitors"] for r in daily[:7]) / min(n, 7)) if n else 0,
        "avg30": round(sum(r["visitors"] for r in daily) / n) if n else 0,
        "views30": sum(r["views"] for r in daily),
        "peak": max((r["visitors"] for r in daily), default=1) or 1,
        "days": days,
    }
