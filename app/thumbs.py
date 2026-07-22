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
# originals above this get a display derivative too — multi-MB Reddit media
# made detail pages feel sluggish (the original stays for download/analysis)
DISPLAY_BYTES = 400 * 1024


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


_PREF_FIELDS = {
    "device": ("make", "model", "lens", "software", "encoder"),
    "time": ("captured_at", "subsec", "utc_offset"),
    "location": ("gps_lat", "gps_lon", "gps_altitude_m", "compass_deg", "compass_ref"),
}


def apply_exif_prefs(meta: dict, prefs: dict) -> dict:
    """Honor the reporter's per-file metadata consent from the wizard."""
    out = dict(meta)
    for category, fields in _PREF_FIELDS.items():
        if not prefs.get(category, True):
            for f in fields:
                out.pop(f, None)
    return out


def strip_video_metadata(url: str) -> bytes:
    """Lossless re-mux without container metadata (streams untouched) — used
    when the reporter excluded location, since the served file itself would
    otherwise leak the GPS tags."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url,
         "-map_metadata", "-1", "-c", "copy", "-f", "mp4",
         "-movflags", "+faststart", "pipe:1"],
        capture_output=True, timeout=600,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"metadata strip failed: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout


def process_pending(conn, limit: int = 3, oldest_first: bool = False) -> int:
    # Newest-first by default: a fresh submission must never wait behind a
    # backfill backlog. Bulk burners pass oldest_first=True so a second
    # process drains from the other end without contention.
    order = "m.id" if oldest_first else "m.id DESC"
    rows = conn.execute(
        f"""SELECT m.id, m.r2_key, m.kind, m.exif_prefs, s.source FROM media m
           JOIN sightings s ON s.id = m.sighting_id
           WHERE m.thumb_key IS NULL AND m.thumb_attempts < 2
           ORDER BY {order} LIMIT ?""",
        (limit,),
    ).fetchall()
    done = 0
    for row in rows:
        conn.execute("UPDATE media SET thumb_attempts = thumb_attempts + 1 WHERE id=?", (row["id"],))
        conn.commit()
        try:
            prefs = json.loads(row["exif_prefs"]) if row["exif_prefs"] else {}
            hide_location = not prefs.get("location", True)
            url = r2.public_url(row["r2_key"])
            display_key = None
            meta: dict = {}
            if row["kind"] == "image":
                resp = httpx.get(url, timeout=60)
                resp.raise_for_status()
                thumb = generate_image_thumb(resp.content)
                meta = mediameta.extract_image_meta(resp.content)
                # a display derivative (Pillow re-save = EXIF-free) is needed
                # for HEIC always, for ANY image whose location the reporter
                # excluded (the original file itself carries GPS), and for
                # oversized originals that would make the viewer sluggish
                # (except GIFs — a JPEG derivative would freeze the animation)
                oversized = (len(resp.content) > DISPLAY_BYTES
                             and not row["r2_key"].lower().endswith(".gif"))
                if needs_display_derivative(row["r2_key"]) or hide_location or oversized:
                    display = generate_image_thumb(resp.content, DISPLAY_MAX, 90)
                    rest = row["r2_key"].split("/", 1)[1]
                    display_key = "display/" + rest.rsplit(".", 1)[0] + ".jpg"
                    r2.put_bytes(display_key, display, "image/jpeg")
            else:
                thumb = generate_video_poster(url)
                meta = mediameta.extract_video_meta(url)
                if hide_location and meta.get("gps_lat") is not None:
                    # replace the served file with a metadata-free remux
                    clean_bytes = strip_video_metadata(url)
                    r2.put_bytes(row["r2_key"], clean_bytes, "video/mp4")
            meta = apply_exif_prefs(meta, prefs)
            tkey = thumb_key_for(row["r2_key"])
            r2.put_bytes(tkey, thumb, "image/jpeg")
            conn.execute(
                "UPDATE media SET thumb_key=?, display_key=?, exif_json=? WHERE id=?",
                (tkey, display_key, json.dumps(meta) if meta else None, row["id"]))
            conn.commit()
            if row["source"] == "site" and prefs.get("device", True):
                _autofill_capture_device(conn, row["id"], meta)
            done += 1
        except Exception as exc:
            print(f"thumbs: media {row['id']} failed: {exc}")
    return done


def process_sky_events(conn, limit: int = 2) -> int:
    """Compute overhead-satellite context for recent geocoded sightings.
    Runs in the background worker: each computation walks the full Starlink
    catalog (a few seconds). Results (or a dated 'unchecked' marker) are
    stored once — no retries, no request-path work."""
    from app import satellites
    rows = conn.execute(
        """SELECT id, lat, lon, sighted_at FROM sightings
           WHERE sky_events IS NULL AND lat IS NOT NULL
             AND status IN ('live', 'deleted_by_user', 'removed_on_reddit')
             AND sighted_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-21 days')
           ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return 0
    try:
        satellites.fetch_today()
    except Exception as exc:
        print(f"sky: TLE fetch failed (using cache): {exc}")
    done = 0
    for r in rows:
        try:
            out = satellites.passes_for(r["lat"], r["lon"], r["sighted_at"])
        except Exception as exc:
            out = {"checked": False, "reason": f"computation failed: {exc}"[:160]}
        conn.execute("UPDATE sightings SET sky_events=? WHERE id=?",
                     (json.dumps(out), r["id"]))
        conn.commit()
        # the bot's pinned comment shipped before this data existed — fold it in
        try:
            from app import posting
            if posting.refresh_sky_comment(conn, r["id"]):
                print(f"sky: updated pinned comment for sighting {r['id']}")
        except Exception as exc:  # never let a Reddit hiccup stall the worker
            print(f"sky: comment refresh failed for {r['id']} (non-fatal): {exc}")
        done += 1
    return done


def start_worker(stop_event: threading.Event) -> threading.Thread:
    def run():
        conn = db.connect(get_settings().db_path)
        while not stop_event.is_set():
            try:
                busy = process_pending(conn)
                busy += process_sky_events(conn)
                if busy == 0:
                    stop_event.wait(10)
            except Exception as exc:
                print(f"thumbs: worker error: {exc}")
                stop_event.wait(30)
        conn.close()

    thread = threading.Thread(target=run, name="thumb-worker", daemon=True)
    thread.start()
    return thread
