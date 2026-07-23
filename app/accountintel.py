"""Deep-dive on a reporter's Reddit account for the verify gate + review panel.

Age-based CQS misses "aged accounts": old accounts that sat dormant and were
reactivated (often bought) for coordinated or disinfo use. Age looks fine; the
tell is age *minus activity* — a long silence followed by a recent burst, thin
karma for the account's age, a narrow subreddit footprint. Since the read token
is a moderator, we can pull the full activity timeline and measure that.

assess() returns everything the verify route and the review panel need:
- exists / banned / hard-gate reason (same thresholds as quality.py)
- age, karma, karma-per-year
- dormancy gap, recent-activity share, reactivation flag
- top subreddits, removed-content count
- an LLM one-liner summarising the account (best-effort)
- route_to_review + a human-readable reason

It never raises: any Reddit/LLM failure degrades to a partial profile, and the
gate fails OPEN (a blip on our side must not wall out legitimate reporters).
"""
import time

import httpx

from app import quality, reddit
from app.config import get_settings

_YEAR = 365.25 * 86400


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — intel is best-effort
        print(f"accountintel: {getattr(fn, '__name__', 'call')} failed: {exc}")
        return default


def _timeline_signals(activity: list[dict]) -> dict:
    """Dormancy gap + recent-activity share from a newest-first timeline."""
    stamps = sorted((a["created_utc"] for a in activity if a["created_utc"]), reverse=True)
    now = time.time()
    out = {"activity_count": len(stamps), "recent_share_30d": None,
           "dormancy_gap_days": None, "days_since_last": None,
           "oldest_seen_days": None}
    if not stamps:
        return out
    out["days_since_last"] = int((now - stamps[0]) / 86400)
    out["oldest_seen_days"] = int((now - stamps[-1]) / 86400)
    recent = sum(1 for t in stamps if now - t <= 30 * 86400)
    out["recent_share_30d"] = round(recent / len(stamps), 2)
    # largest silence between consecutive actions (stamps are newest-first)
    gap = 0.0
    for newer, older in zip(stamps, stamps[1:]):
        gap = max(gap, newer - older)
    out["dormancy_gap_days"] = int(gap / 86400)
    return out


def _subreddit_footprint(activity: list[dict], top: int = 8) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for a in activity:
        sr = a.get("subreddit")
        if sr:
            counts[sr] = counts.get(sr, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:top]


def _ai_summary(intel: dict, activity: list[dict]) -> str:
    """One-line LLM read of the account. Best-effort; '' on any failure or when
    no LLM is configured."""
    s = get_settings()
    if not s.llm_api_key:
        return ""
    subs = ", ".join(f"{sr} ({n})" for sr, n in intel.get("top_subreddits", [])[:8])
    samples = "\n".join(f"- [{a['subreddit']}] {a['text']}"
                        for a in activity[:15] if a.get("text"))
    facts = (f"account_age_days={intel.get('age_days')}, "
             f"karma_per_year={intel.get('karma_per_year')}, "
             f"total_karma={intel.get('total_karma')}, "
             f"visible_activity={intel.get('activity_count')}, "
             f"recent_30d_share={intel.get('recent_share_30d')}, "
             f"largest_dormancy_gap_days={intel.get('dormancy_gap_days')}, "
             f"days_since_last_activity={intel.get('days_since_last')}")
    system = (
        "You are a Reddit moderation assistant for r/UFOs. Given an account's "
        "signals and a sample of its recent activity, write 2-3 short, factual "
        "sentences summarising the account for a human moderator. Call out any "
        "signs of a reactivated aged account (long dormancy then a recent burst), "
        "karma farming, bot/spam behaviour, or coordinated/disinformation posting. "
        "Stay neutral and do not invent facts not supported by the data."
        + (" /no_think" if s.llm_reasoning_off else ""))
    user = f"Signals: {facts}\n\nTop subreddits: {subs}\n\nRecent activity:\n{samples}"
    try:
        resp = httpx.post(
            s.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
            json={"model": s.llm_model, "temperature": 0.2, "max_tokens": 220,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=30)
        if resp.status_code != 200:
            return ""
        content = resp.json()["choices"][0]["message"].get("content") or ""
        return content.strip()[:800]
    except Exception as exc:  # noqa: BLE001
        print(f"accountintel: AI summary failed: {exc}")
        return ""


def assess(username: str) -> dict:
    """Full assessment. Always returns a dict; never raises."""
    s = get_settings()
    intel: dict = {"username": username, "exists": None, "route_to_review": False,
                   "reason": "", "flags": []}

    banned = _safe(lambda: reddit.is_banned(username), default=False)
    try:
        about = reddit.user_about(username)
    except reddit.RedditError:
        # can't read the account — fail OPEN, but note we couldn't verify
        intel["reason"] = ""
        return intel
    if banned:
        intel.update(exists=True, route_to_review=True,
                     reason=f"🚫 BANNED on r/{s.subreddit} — do not approve (ban evasion)")
        intel["flags"].append("banned")
        return intel
    if about is None:
        intel.update(exists=False, route_to_review=True,
                     reason="account not found (suspended, shadowbanned, or deleted)")
        return intel

    intel["exists"] = True
    created = about.get("created_utc")
    age_days = int((time.time() - created) / 86400) if created else None
    link = about.get("link_karma", 0) or 0
    comment = about.get("comment_karma", 0) or 0
    total = about.get("total_karma") or (link + comment)
    age_years = (time.time() - created) / _YEAR if created else None
    intel.update(age_days=age_days, link_karma=link, comment_karma=comment,
                 total_karma=total, has_verified_email=bool(about.get("has_verified_email")),
                 karma_per_year=round(total / age_years, 1) if age_years and age_years > 0.1 else None)

    # hard CQS signals (same thresholds as the standalone gate)
    hard = quality.check_about(about)

    # activity deep-dive
    if s.account_intel_enabled:
        activity = _safe(lambda: reddit.user_activity(username), default=[])
        intel.update(_timeline_signals(activity))
        intel["top_subreddits"] = _subreddit_footprint(activity)
        intel["removed_seen"] = sum(1 for a in activity if a.get("removed"))

        # aged-account composite: an OLD account (age passes CQS) that either
        # went dormant then came back, or barely participates for its age.
        aged = age_days is not None and age_days > s.cqs_min_account_age_days
        reactivated = (
            intel.get("dormancy_gap_days") is not None
            and intel["dormancy_gap_days"] >= s.intel_dormancy_gap_days
            and intel.get("days_since_last") is not None
            and intel["days_since_last"] <= s.intel_reactivation_recent_days)
        thin = (intel.get("karma_per_year") is not None
                and intel["karma_per_year"] < s.intel_min_karma_per_year)
        if aged and reactivated:
            intel["flags"].append(
                f"reactivated: silent {intel['dormancy_gap_days']}d, "
                f"active again {intel['days_since_last']}d ago")
        if aged and thin:
            intel["flags"].append(f"thin for age: {intel['karma_per_year']} karma/yr")
        if intel.get("recent_share_30d") == 1.0 and aged and intel["activity_count"] >= 3:
            intel["flags"].append("all visible activity is in the last 30 days")

        intel["summary"] = _ai_summary(intel, activity)
        # aged + (reactivated OR thin) is the composite worth a human look
        if aged and (reactivated or thin):
            intel["route_to_review"] = True
            intel["reason"] = "aged account with reactivation/low-participation signals"

    # a hard CQS failure always wins (more specific reason)
    if hard:
        intel["route_to_review"] = True
        intel["reason"] = hard
        intel["flags"].insert(0, hard)

    return intel
