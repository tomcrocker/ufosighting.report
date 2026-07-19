import json
import re
from datetime import date as _date, datetime, timezone

import httpx

from app import helpers
from app.config import get_settings

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


SYSTEM_PROMPT = (
    "You extract structured UFO-sighting metadata from a Reddit post. "
    "Return ONLY a JSON object with keys: date (YYYY-MM-DD), time (HH:MM 24h), "
    "timezone (IANA name like America/Vancouver), location_text, city, country, "
    "shape, num_objects (one of 1,2,3,4,5+), duration_seconds (integer), "
    "summary (one neutral sentence). Use null for any field NOT explicitly "
    "stated in the text — do NOT guess or invent. shape must be one of: "
    + ", ".join(helpers.SHAPES) + ". "
    "If the message begins with a line '[POST DATE] YYYY-MM-DD' (when the post "
    "was submitted), resolve relative or year-less dates against it: "
    "'yesterday'/'last night' = the day before or the night of [POST DATE]; a "
    "weekday like 'last Thursday' = the most recent such weekday on or before "
    "[POST DATE]; a month+day with no year = the most recent occurrence on or "
    "before [POST DATE]; 'just saw'/'right now'/'at the moment' = [POST DATE]. "
    "The sighting date is never after [POST DATE]. If the post only relays "
    "someone else's undated account, or no date is stated or implied, use null."
)


def _parse_json_content(content) -> dict:
    """Parse a chat completion's content into a dict, tolerating markdown
    fences (```json ... ```) that some models wrap JSON in."""
    if not isinstance(content, str):
        return {}
    c = content.strip()
    if c.startswith("```"):
        c = c[3:]
        if c[:4].lower() == "json":
            c = c[4:]
        if c.endswith("```"):
            c = c[:-3]
        c = c.strip()
    try:
        data = json.loads(c)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _call_llm(text: str, *, base_url: str, api_key: str, model: str,
              reasoning_off: bool, timeout: float = 45,
              max_tokens: int = 1024) -> dict:
    """Provider-agnostic OpenAI-compatible chat call. Returns the raw parsed
    JSON dict (pre-validation), or {} on any failure.

    `reasoning_off` appends the Qwen/Nemotron `/no_think` token so reasoning
    models emit the JSON directly instead of filling `reasoning_content`.
    `max_tokens` bounds cost and stops a reasoning model from running away if
    the off-switch is ever missing (our schema needs <200 tokens)."""
    system = SYSTEM_PROMPT + (" /no_think" if reasoning_off else "")
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return {}
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_json_content(content)
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return {}


def extract_fields(text: str, *, post_date: str | None = None) -> dict:
    """Extract sighting fields from combined post text. `post_date` (YYYY-MM-DD,
    the post's submission date) is prepended as a [POST DATE] marker so the model
    can resolve relative/year-less dates against it (see SYSTEM_PROMPT)."""
    s = get_settings()
    if not s.llm_api_key:
        return {}
    user = f"[POST DATE] {post_date[:10]}\n{text}" if post_date else text
    return _call_llm(
        user,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        reasoning_off=s.llm_reasoning_off,
    )
