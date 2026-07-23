import time

import pytest

from app import quality


def _about(**over):
    base = {"created_utc": time.time() - 400 * 86400, "total_karma": 5000,
            "link_karma": 2000, "comment_karma": 3000, "has_verified_email": True,
            "is_suspended": False}
    base.update(over)
    return base


@pytest.fixture
def stub(monkeypatch):
    """Default: not banned, healthy account. Tests override pieces."""
    state = {"banned": False, "about": _about()}
    monkeypatch.setattr(quality.reddit, "is_banned", lambda u: state["banned"])
    monkeypatch.setattr(quality.reddit, "user_about", lambda u: state["about"])
    return state


def test_healthy_account_passes(stub):
    ok, reason = quality.gate("gooduser")
    assert ok and reason == ""


def test_banned_user_is_blocked_first(stub):
    stub["banned"] = True
    stub["about"] = _about()  # otherwise pristine — ban must still win
    ok, reason = quality.gate("banned")
    assert not ok and "BANNED" in reason


def test_new_account_goes_to_review(stub):
    stub["about"] = _about(created_utc=time.time() - 3 * 86400)
    ok, reason = quality.gate("newbie")
    assert not ok and "3 days old" in reason


def test_negative_karma_blocked(stub):
    stub["about"] = _about(comment_karma=-42, total_karma=-42, link_karma=0)
    ok, reason = quality.gate("troll")
    assert not ok and "negative karma" in reason


def test_low_total_karma_goes_to_review(stub):
    stub["about"] = _about(total_karma=3, link_karma=1, comment_karma=2)
    ok, reason = quality.gate("thin")
    assert not ok and "low total karma" in reason


def test_deleted_or_shadowbanned_account_blocked(stub):
    stub["about"] = None  # 404 from user_about
    ok, reason = quality.gate("ghost")
    assert not ok and "not found" in reason


def test_suspended_account_blocked(stub):
    stub["about"] = _about(is_suspended=True)
    ok, reason = quality.gate("suspended")
    assert not ok and "suspended" in reason


def test_email_gate_off_by_default(stub):
    stub["about"] = _about(has_verified_email=False)
    assert quality.gate("noemail")[0] is True


def test_email_gate_when_enabled(stub, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CQS_REQUIRE_VERIFIED_EMAIL", "1")
    get_settings.cache_clear()
    stub["about"] = _about(has_verified_email=False)
    ok, reason = quality.gate("noemail")
    get_settings.cache_clear()
    assert not ok and "email not verified" in reason


def test_per_type_floor_when_configured(stub, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CQS_MIN_COMMENT_KARMA", "100")
    get_settings.cache_clear()
    stub["about"] = _about(comment_karma=10, link_karma=9000, total_karma=9010)
    ok, reason = quality.gate("lopsided")
    get_settings.cache_clear()
    assert not ok and "low comment karma" in reason


def test_fails_open_on_api_error(stub, monkeypatch):
    """A Reddit blip must not dump legitimate reporters into review."""
    def boom(u):
        raise quality.reddit.RedditError("503")
    monkeypatch.setattr(quality.reddit, "user_about", boom)
    assert quality.gate("someone")[0] is True


def test_ban_check_error_does_not_block(stub, monkeypatch):
    def boom(u):
        raise quality.reddit.RedditError("scope?")
    monkeypatch.setattr(quality.reddit, "is_banned", boom)
    # ban check failed, but the account is otherwise healthy -> still allowed
    assert quality.gate("someone")[0] is True
