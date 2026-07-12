import io
import json
import subprocess
import threading

import httpx
from PIL import Image, ImageOps

from app import db, mediameta, r2, search
from app.config import get_settings

THUMB_MAX = 640
DISPLAY_MAX = 2048  # browser-viewable derivative for HEIC originals


def generate_image_thumb(data: bytes, max_px: int = THUMB_MAX, quality: int = 82) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_px, max_px))
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def needs_display_derivative(r2_key: str) -> bool:
    # HEIC/HEIF originals are preserved byte-exact but most browsers can't
    # render them — serve a high-res JPEG derivative for viewing instead
    return r2_key.lower().rsplit(".", 1)[-1] in ("heic", "heif")


def _autofill_capture_device(conn, media_id: int, meta: dict) -> None:
    """EXIF knows the camera even when the reporter left the field blank."""
    make, model = meta.get("make", ""), meta.get("model", "")
    if not model:
        return
    device = model if make and model.startswith(make) else f"{make} {model}".strip()
    row = conn.execute(
        """UPDATE sightings SET capture_device=?
           WHERE id = (SELECT sighting_id FROM media WHERE id=?)
             AND source='site' AND (capture_device IS NULL OR capture_device='')
           RETURNING id""", (device[:100], media_id)).fetchone()
    conn.commit()
    if row:
        search.index_sightings(conn, [row["id"]])


def generate_video_poster(url: str) -> bytes:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", "1", "-i", url, "-frames:v", "1",
        "-vf", "scale='min(640,iw)':-2",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg poster failed: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout


def thumb_key_for(r2_key: str) -> str:
    rest = r2_key.split("/", 1)[1]
    return "thumbs/" + rest.rsplit(".", 1)[0] + ".jpg"


def process_pending(conn, limit: int = 3) -> int:
    rows = conn.execute(
        """SELECT m.id, m.r2_key, m.kind, s.source FROM media m
           JOIN sightings s ON s.id = m.sighting_id
           WHERE m.thumb_key IS NULL AND m.thumb_attempts < 2
           ORDER BY m.id LIMIT ?""",
        (limit,),
    ).fetchall()
    done = 0
    for row in rows:
        conn.execute("UPDATE media SET thumb_attempts = thumb_attempts + 1 WHERE id=?", (row["id"],))
        conn.commit()
        try:
            url = r2.public_url(row["r2_key"])
            display_key = None
            meta: dict = {}
            if row["kind"] == "image":
                resp = httpx.get(url, timeout=60)
                resp.raise_for_status()
                thumb = generate_image_thumb(resp.content)
                meta = mediameta.extract_image_meta(resp.content)
                if needs_display_derivative(row["r2_key"]):
                    display = generate_image_thumb(resp.content, DISPLAY_MAX, 90)
                    rest = row["r2_key"].split("/", 1)[1]
                    display_key = "display/" + rest.rsplit(".", 1)[0] + ".jpg"
                    r2.put_bytes(display_key, display, "image/jpeg")
            else:
                thumb = generate_video_poster(url)
                meta = mediameta.extract_video_meta(url)
            tkey = thumb_key_for(row["r2_key"])
            r2.put_bytes(tkey, thumb, "image/jpeg")
            conn.execute(
                "UPDATE media SET thumb_key=?, display_key=?, exif_json=? WHERE id=?",
                (tkey, display_key, json.dumps(meta) if meta else None, row["id"]))
            conn.commit()
            if row["source"] == "site":
                _autofill_capture_device(conn, row["id"], meta)
            done += 1
        except Exception as exc:
            print(f"thumbs: media {row['id']} failed: {exc}")
    return done


def start_worker(stop_event: threading.Event) -> threading.Thread:
    def run():
        conn = db.connect(get_settings().db_path)
        while not stop_event.is_set():
            try:
                if process_pending(conn) == 0:
                    stop_event.wait(10)
            except Exception as exc:
                print(f"thumbs: worker error: {exc}")
                stop_event.wait(30)
        conn.close()

    thread = threading.Thread(target=run, name="thumb-worker", daemon=True)
    thread.start()
    return thread
