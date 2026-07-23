import httpx
import respx

from app import discordhook
from app.config import get_settings
from tests.test_db import _insert_sighting

HOOK = "https://discord.com/api/webhooks/123/abc"


def _enable(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    get_settings.cache_clear()


def test_noop_without_webhook(db_conn, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()
    sid = _insert_sighting(db_conn)
    assert discordhook.notify_new_sighting(db_conn, sid) is False
    get_settings.cache_clear()


@respx.mock
def test_posts_embed_for_new_sighting(db_conn, monkeypatch):
    _enable(monkeypatch)
    route = respx.post(HOOK).mock(return_value=httpx.Response(204))
    sid = _insert_sighting(db_conn, reddit_username="witness1",
                           title="Bright light over Victoria, BC")
    db_conn.execute("INSERT INTO media (sighting_id, r2_key, kind) VALUES (?,?,?)",
                    (sid, "uploads/a.jpg", "image"))
    db_conn.commit()
    assert discordhook.notify_new_sighting(db_conn, sid) is True
    import json
    body = json.loads(route.calls[0].request.content)
    embed = body["embeds"][0]
    assert embed["description"] == "Bright light over Victoria, BC"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Reporter"] == "u/witness1"
    assert fields["Media"] == "1 photo"
    assert fields["Status"] == "Awaiting verification"
    get_settings.cache_clear()


@respx.mock
def test_webhook_failure_is_non_fatal(db_conn, monkeypatch):
    _enable(monkeypatch)
    respx.post(HOOK).mock(return_value=httpx.Response(500))
    sid = _insert_sighting(db_conn)
    assert discordhook.notify_new_sighting(db_conn, sid) is False  # swallowed
    get_settings.cache_clear()
