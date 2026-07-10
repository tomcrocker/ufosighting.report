import io

from PIL import Image

from app import thumbs
from tests.test_db import _insert_sighting


def _png_bytes(w=1600, h=1200) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 40, 50)).save(buf, "PNG")
    return buf.getvalue()


def test_generate_image_thumb_shrinks_to_jpeg():
    out = thumbs.generate_image_thumb(_png_bytes())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) <= 640


def test_thumb_key_for():
    key = "uploads/2026/07/" + "a" * 32 + ".mp4"
    assert thumbs.thumb_key_for(key) == "thumbs/2026/07/" + "a" * 32 + ".jpg"


def test_process_pending_image(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, "uploads/2026/07/" + "b" * 32 + ".png"),
    )
    db_conn.commit()

    class FakeResp:
        content = _png_bytes()
        def raise_for_status(self): pass

    uploaded = {}
    monkeypatch.setattr(thumbs.httpx, "get", lambda url, timeout: FakeResp())
    monkeypatch.setattr(thumbs.r2, "put_bytes", lambda k, d, ct: uploaded.update({k: len(d)}))

    assert thumbs.process_pending(db_conn) == 1
    row = db_conn.execute("SELECT thumb_key, thumb_attempts FROM media").fetchone()
    assert row["thumb_key"] == "thumbs/2026/07/" + "b" * 32 + ".jpg"
    assert row["thumb_attempts"] == 1
    assert row["thumb_key"] in uploaded


def test_process_pending_video_uses_poster(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'video')",
        (sid, "uploads/2026/07/" + "c" * 32 + ".mp4"),
    )
    db_conn.commit()
    monkeypatch.setattr(thumbs, "generate_video_poster", lambda url: b"fake-jpeg")
    monkeypatch.setattr(thumbs.r2, "put_bytes", lambda k, d, ct: None)
    assert thumbs.process_pending(db_conn) == 1


def test_process_pending_gives_up_after_two_attempts(db_conn, monkeypatch):
    sid = _insert_sighting(db_conn)
    db_conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind) VALUES (?, ?, 'image')",
        (sid, "uploads/2026/07/" + "d" * 32 + ".png"),
    )
    db_conn.commit()

    def boom(url, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr(thumbs.httpx, "get", boom)
    assert thumbs.process_pending(db_conn) == 0  # attempt 1
    assert thumbs.process_pending(db_conn) == 0  # attempt 2
    assert thumbs.process_pending(db_conn) == 0  # no more attempts
    row = db_conn.execute("SELECT thumb_key, thumb_attempts FROM media").fetchone()
    assert row["thumb_key"] is None
    assert row["thumb_attempts"] == 2
