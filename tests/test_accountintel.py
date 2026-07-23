import time

import pytest

from app import accountintel


def _about(age_days=2000, total=5000, link=2000, comment=3000, email=True, suspended=False):
    return {"created_utc": time.time() - age_days * 86400, "total_karma": total,
            "link_karma": link, "comment_karma": comment,
            "has_verified_email": email, "is_suspended": suspended}


def _activity(days_ago_list, subreddit="UFOs"):
    now = time.time()
    return [{"kind": "comment", "created_utc": now - d * 86400, "subreddit": subreddit,
             "score": 1, "text": "x", "removed": False} for d in days_ago_list]


@pytest.fixture
def stub(monkeypatch):
    # a healthy, regularly-active account: no long silence (largest gap < 180d)
    state = {"banned": False, "about": _about(),
             "activity": _activity([1, 3, 7, 14, 30, 60, 90, 120, 200, 300])}
    monkeypatch.setattr(accountintel.reddit, "is_banned", lambda u: state["banned"])
    monkeypatch.setattr(accountintel.reddit, "user_about", lambda u: state["about"])
    monkeypatch.setattr(accountintel.reddit, "user_activity", lambda u, **k: state["activity"])
    monkeypatch.setattr(accountintel, "_ai_summary", lambda intel, act: "")  # skip LLM
    return state


def test_healthy_account_passes(stub):
    r = accountintel.assess("good")
    assert r["exists"] is True and r["route_to_review"] is False and r["flags"] == []


def test_nonexistent_account_flagged(stub):
    stub["about"] = None
    r = accountintel.assess("ghost")
    assert r["exists"] is False and r["route_to_review"] and "not found" in r["reason"]


def test_banned_wins(stub):
    stub["banned"] = True
    r = accountintel.assess("banned")
    assert r["route_to_review"] and "BANNED" in r["reason"]


def test_reactivated_aged_account_routed(stub):
    # 6-year-old account, activity only 3 days ago and then a 2-year silence before
    stub["about"] = _about(age_days=2200, total=4000)
    stub["activity"] = _activity([3, 4, 5, 900, 905])  # ~2.4y gap, active 3d ago
    r = accountintel.assess("sleeper")
    assert r["route_to_review"] is True
    assert any("reactivated" in f for f in r["flags"])
    assert r["dormancy_gap_days"] > 180 and r["days_since_last"] <= 45


def test_thin_for_age_routed(stub):
    stub["about"] = _about(age_days=3000, total=40, link=10, comment=30)  # ~5 karma/yr
    stub["activity"] = _activity([2, 400, 1200])
    r = accountintel.assess("farmed")
    assert r["route_to_review"] is True
    assert any("thin for age" in f for f in r["flags"])


def test_hard_cqs_reason_wins_over_aged(stub):
    stub["about"] = _about(age_days=10)  # brand new -> hard gate
    stub["activity"] = _activity([1, 2])
    r = accountintel.assess("newbie")
    assert r["route_to_review"] and "new account" in r["reason"]


def test_never_raises_on_api_error(stub, monkeypatch):
    def boom(u):
        raise accountintel.reddit.RedditError("down")
    monkeypatch.setattr(accountintel.reddit, "user_about", boom)
    r = accountintel.assess("x")
    assert r["route_to_review"] is False  # fails open


def test_timeline_signals_math():
    now = time.time()
    stamps = _activity([1, 10, 400])  # gap of ~390 days
    sig = accountintel._timeline_signals(stamps)
    assert sig["activity_count"] == 3
    assert sig["days_since_last"] <= 1
    assert 385 <= sig["dormancy_gap_days"] <= 395
    assert sig["recent_share_30d"] == round(2 / 3, 2)  # two of three within 30d
