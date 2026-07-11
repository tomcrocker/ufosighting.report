import json

import backfill_archive
from app import extract, geocode


def _row(pid="arc1", **over):
    d = {"id": pid, "title": "Lights over Ely", "author": "witness7",
         "selftext": "Three amber lights in formation.", "created_utc": 1735000000,
         "score": 55, "num_comments": 12, "url": f"https://v.redd.it/{pid}",
         "op_comments": ["It hovered for two minutes"],
         "top_comments": [
             {"id": "ca", "author": "alice", "body": "**wow**", "score": 30,
              "created_utc": 1735000100, "permalink": "/r/UFOs/comments/arc1/x/ca/"},
             {"id": "cb", "author": "bob", "body": "starlink", "score": 5,
              "created_utc": 1735000200, "permalink": ""},
         ],
         "media": [{"key": "uploads/arc/arc1_0.mp4", "kind": "video"},
                   {"key": "uploads/arc/arc1_1.jpg", "kind": "image"}],
         "yt_url": None, "media_error": None}
    d.update(over)
    return d


def _stub(monkeypatch, clamped_extra=None):
    seen = {}
    monkeypatch.setattr(extract, "extract_fields", lambda text: seen.update(text=text) or {})
    clamped = {k: None for k in ("date", "time", "timezone", "location_text", "city",
                                 "country", "shape", "num_objects", "duration_seconds",
                                 "summary")}
    clamped.update(clamped_extra or {})
    monkeypatch.setattr(extract, "validate_and_clamp",
                        lambda raw, post_created_iso: clamped)
    monkeypatch.setattr(geocode, "forward", lambda conn, q: None)
    return seen


def test_ingests_row_with_media_comments_and_no_api(db_conn, monkeypatch):
    seen = _stub(monkeypatch)
    stats = backfill_archive.run(db_conn, [_row()])
    assert stats == {"rows": 1, "added": 1, "skipped_existing": 0}
    s = db_conn.execute("SELECT * FROM sightings WHERE reddit_post_id='arc1'").fetchone()
    assert s["source"] == "reddit" and s["status"] == "live"
    assert s["reddit_score"] == 55 and s["reddit_num_comments"] == 12
    media = db_conn.execute("SELECT * FROM media WHERE sighting_id=? ORDER BY sort_order",
                            (s["id"],)).fetchall()
    assert [(m["r2_key"], m["kind"], m["sort_order"]) for m in media] == [
        ("uploads/arc/arc1_0.mp4", "video", 0), ("uploads/arc/arc1_1.jpg", "image", 1)]
    cmts = db_conn.execute("SELECT * FROM comments WHERE sighting_id=? ORDER BY score DESC",
                           (s["id"],)).fetchall()
    assert [c["author"] for c in cmts] == ["alice", "bob"]
    # op comment fed into the extraction text
    assert "It hovered for two minutes" in seen["text"]


def test_dedup_skips_existing(db_conn, monkeypatch):
    _stub(monkeypatch)
    backfill_archive.run(db_conn, [_row()])
    stats = backfill_archive.run(db_conn, [_row()])
    assert stats == {"rows": 1, "added": 0, "skipped_existing": 1}
    assert db_conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 1


def test_youtube_row_queues_yt_job(db_conn, monkeypatch):
    _stub(monkeypatch)
    row = _row("arcyt", media=[], url="https://reddit.com/r/UFOs/comments/arcyt/x/",
               selftext="clip: https://youtu.be/XHWPQEJ_TVA",
               yt_url="https://www.youtube.com/watch?v=XHWPQEJ_TVA")
    backfill_archive.run(db_conn, [row])
    job = db_conn.execute("SELECT j.url FROM yt_jobs j JOIN sightings s ON s.id=j.sighting_id "
                          "WHERE s.reddit_post_id='arcyt'").fetchone()
    assert job["url"] == "https://www.youtube.com/watch?v=XHWPQEJ_TVA"


def test_text_only_row_ingests(db_conn, monkeypatch):
    _stub(monkeypatch)
    row = _row("arctxt", media=[], top_comments=[], op_comments=[],
               media_error="download failed: 404")
    stats = backfill_archive.run(db_conn, [row])
    assert stats["added"] == 1
    sid = db_conn.execute("SELECT id FROM sightings WHERE reddit_post_id='arctxt'").fetchone()[0]
    assert db_conn.execute("SELECT COUNT(*) FROM media WHERE sighting_id=?", (sid,)).fetchone()[0] == 0


def test_main_reads_jsonl(tmp_path, db_conn, monkeypatch):
    _stub(monkeypatch)
    p = tmp_path / "m.jsonl"
    p.write_text(json.dumps(_row("arcj1")) + "\n" + json.dumps(_row("arcj2")) + "\n")
    rows = backfill_archive.load_manifest(str(p))
    assert [r["id"] for r in rows] == ["arcj1", "arcj2"]
