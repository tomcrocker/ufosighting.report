"""AI-standardized post titles.

Reporters pick their own titles — often clickbait, all-caps, or vague. This
rewrites the reporter's title plus the structured sighting details into one
clean, factual "<description> over <location>" line, then appends the date/time
deterministically (so the date format is always consistent and never
hallucinated). Best-effort: any LLM failure keeps the reporter's title unchanged.
"""
import httpx

from app import helpers
from app.config import get_settings

_SYSTEM = (
    "You title UFO sighting posts for r/UFOs. Given a reporter's raw title and "
    "the structured details of their sighting, write ONE clean, factual title "
    "describing what the object looked like and how it moved, plus the location, "
    "phrased naturally (for example: 'Silent white capsule-shaped object shot up "
    "over Rochester, NY'). Rules: no hype, no all-caps, no clickbait, no "
    "exclamation marks, no quotation marks or emoji. Describe the actual "
    "appearance rather than vague labels — avoid 'UFO' and only use 'orb' if the "
    "reporter clearly means a literal glowing sphere. Do NOT include the date or "
    "time. Keep it under 160 characters. Output only the title text, nothing else.")


def _sanitize(text: str) -> str:
    text = (text or "").strip().strip('"').strip("'").strip()
    text = text.split("\n")[0].strip()          # first line only
    for prefix in ("Title:", "title:", "Standardized title:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip('"').strip()[:200]


def generate(user_title: str, clean: dict) -> str:
    """Return a standardized title, or the reporter's original on any failure."""
    s = get_settings()
    date_part = helpers.sighting_title_date(clean.get("sighted_at"), clean.get("tz_name"))
    if not s.ai_titles_enabled or not s.llm_api_key:
        return user_title

    details = []
    if clean.get("num_objects"):
        details.append(f"objects: {clean['num_objects']}")
    if clean.get("shape"):
        details.append(f"shape: {clean['shape']}")
    if clean.get("movement"):
        details.append("movement: " + ", ".join(clean["movement"]))
    location = clean.get("location_text") or clean.get("city") or ""
    user_msg = (f"Reporter's title: {user_title}\n"
                f"Details: {'; '.join(details) or 'n/a'}\n"
                f"Location: {location or 'unknown'}\n"
                f"Description: {(clean.get('description') or '')[:600]}")
    system = _SYSTEM + (" /no_think" if s.llm_reasoning_off else "")
    try:
        resp = httpx.post(
            s.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
            json={"model": s.llm_model, "temperature": 0.3, "max_tokens": 80,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user_msg}]},
            timeout=30)
        if resp.status_code != 200:
            return user_title
        core = _sanitize(resp.json()["choices"][0]["message"].get("content"))
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"titlegen: failed, keeping reporter title: {exc}")
        return user_title
    if not core:
        return user_title
    title = f"{core} — {date_part}" if date_part else core
    return title[:300]  # Reddit's title cap
