import io

from PIL import Image

import burn_displays
from app import r2
from tests.test_db import _insert_sighting


def _img_bytes(px, quality=95):
    img = Image.new("RGB", (px, px), (30, 40, 50))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def _seed_media(conn, key, size_bytes=None, kind="image"):
    sid = _insert_sighting(conn)
    cur = conn.execute(
        "INSERT INTO media (sighting_id, r2_key, kind, size_bytes) VALUES (?,?,?,?)",
        (sid, key, kind, size_bytes))
    conn.commit()
    return cur.lastrowid


def test_candidates_skip_gifs_small_and_derived(db_conn):
    big = _seed_media(db_conn, "uploads/arc/a.jpg")                       # unknown size
    _seed_media(db_conn, "uploads/arc/b.gif")                             # gif: never
    _seed_media(db_conn, "uploads/arc/c.jpg", size_bytes=100_000)         # known-small
    known_big = _seed_media(db_conn, "uploads/arc/d.jpg", size_bytes=5_000_000)
    ids = [r["id"] for r in burn_displays.candidates(db_conn)]
    assert big in ids and known_big in ids
    assert len(ids) == 2


def test_process_derives_oversized_and_records_size(db_conn, monkeypatch):
    stored = {}
    monkeypatch.setattr(r2, "put_bytes", lambda k, b, ct: stored.update({k: b}))
    mid = _seed_media(db_conn, "uploads/arc/big.png")
    # 3000px high-quality JPEG > DISPLAY_BYTES threshold
    data = _img_bytes(3000)
    while len(data) <= burn_displays.DISPLAY_BYTES:   # ensure over threshold
        data += b"\0" * 100_000
    out = burn_displays.process_one(db_conn, {"id": mid, "r2_key": "uploads/arc/big.png"},
                                    fetch=lambda url: data)
    assert out == "derived"
    row = db_conn.execute("SELECT display_key, size_bytes FROM media WHERE id=?",
                          (mid,)).fetchone()
    assert row["display_key"] == "display/arc/big.jpg"
    assert row["size_bytes"] == len(data)
    assert row["display_key"] in stored          # derivative actually uploaded


def test_process_small_records_size_no_derivative(db_conn, monkeypatch):
    monkeypatch.setattr(r2, "put_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no upload")))
    mid = _seed_media(db_conn, "uploads/arc/small.jpg")
    out = burn_displays.process_one(db_conn, {"id": mid, "r2_key": "uploads/arc/small.jpg"},
                                    fetch=lambda url: _img_bytes(400))
    assert out == "small"
    row = db_conn.execute("SELECT display_key, size_bytes FROM media WHERE id=?",
                          (mid,)).fetchone()
    assert row["display_key"] is None and row["size_bytes"] > 0
    # now known-small -> drops out of the candidate queue
    assert mid not in [r["id"] for r in burn_displays.candidates(db_conn)]
