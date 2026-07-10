import io
import subprocess
import threading

import httpx
from PIL import Image, ImageOps

from app import db, r2
from app.config import get_settings

THUMB_MAX = 640


def generate_image_thumb(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_MAX, THUMB_MAX))
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


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
        """SELECT id, r2_key, kind FROM media
           WHERE thumb_key IS NULL AND thumb_attempts < 2
           ORDER BY id LIMIT ?""",
        (limit,),
    ).fetchall()
    done = 0
    for row in rows:
        conn.execute("UPDATE media SET thumb_attempts = thumb_attempts + 1 WHERE id=?", (row["id"],))
        conn.commit()
        try:
            url = r2.public_url(row["r2_key"])
            if row["kind"] == "image":
                resp = httpx.get(url, timeout=60)
                resp.raise_for_status()
                thumb = generate_image_thumb(resp.content)
            else:
                thumb = generate_video_poster(url)
            tkey = thumb_key_for(row["r2_key"])
            r2.put_bytes(tkey, thumb, "image/jpeg")
            conn.execute("UPDATE media SET thumb_key=? WHERE id=?", (tkey, row["id"]))
            conn.commit()
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
