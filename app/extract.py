import re
from datetime import date as _date, datetime, timezone

from app import helpers

MAX_SOURCE = 2000
MAX_TOTAL = 6500
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def combine_post_text(post: dict, op_comments: list[str]) -> str:
    parts = [f"[TITLE] {(post.get('title') or '')[:MAX_SOURCE]}"]
    body = (post.get("selftext") or "").strip()
    if body:
        parts.append(f"[BODY] {body[:MAX_SOURCE]}")
    for c in op_comments:
        c = (c or "").strip()
        if c:
            parts.append(f"[OP COMMENT] {c[:MAX_SOURCE]}")
    return "\n".join(parts)[:MAX_TOTAL]


def _clean_str(v, cap):
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:cap] if v else None


def validate_and_clamp(raw: dict, *, post_created_iso: str) -> dict:
    out = {k: None for k in (
        "date", "time", "timezone", "location_text", "city", "country",
        "shape", "num_objects", "duration_seconds", "summary")}
    if not isinstance(raw, dict):
        return out

    d = raw.get("date")
    if isinstance(d, str) and _DATE_RE.match(d.strip()):
        try:
            parsed = datetime.strptime(d.strip(), "%Y-%m-%d").date()
            if _date(1940, 1, 1) <= parsed <= datetime.now(timezone.utc).date():
                out["date"] = d.strip()
        except ValueError:
            pass

    t = raw.get("time")
    if isinstance(t, str) and _TIME_RE.match(t.strip()):
        hh, mm = t.strip().split(":")
        out["time"] = f"{int(hh):02d}:{mm}"

    tz = raw.get("timezone")
    if isinstance(tz, str) and tz.strip():
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz.strip())
            out["timezone"] = tz.strip()
        except Exception:
            pass

    out["location_text"] = _clean_str(raw.get("location_text"), 300)
    out["city"] = _clean_str(raw.get("city"), 120)
    out["country"] = _clean_str(raw.get("country"), 120)
    out["summary"] = _clean_str(raw.get("summary"), 500)

    shape = raw.get("shape")
    if isinstance(shape, str) and shape.strip().lower() in helpers.SHAPES:
        out["shape"] = shape.strip().lower()

    n = raw.get("num_objects")
    if isinstance(n, str) and n.strip() in helpers.NUM_OBJECTS:
        out["num_objects"] = n.strip()
    elif isinstance(n, int) and str(n) in helpers.NUM_OBJECTS:
        out["num_objects"] = str(n)

    dur = raw.get("duration_seconds")
    if isinstance(dur, bool):
        dur = None
    if isinstance(dur, (int, float)) and 1 <= int(dur) <= 86400:
        out["duration_seconds"] = int(dur)

    return out
