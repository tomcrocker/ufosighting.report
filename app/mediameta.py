"""Technical metadata extraction from ORIGINAL uploads — the analysis story:
Reddit strips/transcodes everything, but our R2 originals keep EXIF and codec
data, so the site can surface device, optics, timestamps, and encoding facts.

Images: Pillow EXIF (HEIC via pillow-heif). Videos: ffprobe over the R2 URL.
Everything is best-effort — missing/corrupt metadata yields {}.
"""
import io
import json
import subprocess

from PIL import ExifTags, Image

try:  # HEIC/HEIF support — registers .heic decoding with Pillow
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover — dev envs without the wheel
    pass

# curated EXIF fields worth showing; anything else is noise for this use-case
_EXIF_FIELDS = {
    "Make": "make",
    "Model": "model",
    "LensModel": "lens",
    "DateTimeOriginal": "captured_at",
    "OffsetTimeOriginal": "utc_offset",
    "ExposureTime": "exposure",
    "FNumber": "f_number",
    "ISOSpeedRatings": "iso",
    "PhotographicSensitivity": "iso",
    "FocalLength": "focal_length_mm",
    "Software": "software",
}


def _ratio(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _gps_decimal(dms, ref) -> float | None:
    try:
        deg = _ratio(dms[0]) + _ratio(dms[1]) / 60 + _ratio(dms[2]) / 3600
        return round(-deg if ref in ("S", "W") else deg, 6)
    except (TypeError, IndexError, ZeroDivisionError):
        return None


def extract_image_meta(data: bytes) -> dict:
    try:
        img = Image.open(io.BytesIO(data))
        out = {"width": img.width, "height": img.height, "format": img.format}
        exif = img.getexif()
    except Exception:
        return {}
    merged = dict(exif)
    try:
        merged.update(exif.get_ifd(ExifTags.IFD.Exif))
    except Exception:
        pass
    for tag_id, value in merged.items():
        name = ExifTags.TAGS.get(tag_id)
        key = _EXIF_FIELDS.get(name)
        if not key or key in out:
            continue
        if name == "ExposureTime":
            f = _ratio(value)
            if f and f < 1:
                value = f"1/{round(1 / f)}s"
            elif f:
                value = f"{f}s"
        elif name in ("FNumber", "FocalLength"):
            value = _ratio(value)
        out[key] = str(value).strip() if not isinstance(value, (int, float)) else value
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if gps:
            lat = _gps_decimal(gps.get(2), gps.get(1))
            lon = _gps_decimal(gps.get(4), gps.get(3))
            if lat is not None and lon is not None:
                out["gps_lat"], out["gps_lon"] = lat, lon
    except Exception:
        pass
    return out


def extract_video_meta(url: str) -> dict:
    """ffprobe the R2 original (streams via range requests, no full download)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", url],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {}
    out: dict = {}
    fmt = data.get("format", {})
    tags = fmt.get("tags", {}) or {}
    if fmt.get("duration"):
        out["duration_s"] = round(float(fmt["duration"]), 2)
    if fmt.get("bit_rate"):
        out["bitrate_kbps"] = round(int(fmt["bit_rate"]) / 1000)
    for src_key, dst in (("creation_time", "captured_at"), ("encoder", "encoder"),
                         ("com.apple.quicktime.model", "model"),
                         ("com.apple.quicktime.make", "make"),
                         ("com.apple.quicktime.software", "software")):
        if tags.get(src_key):
            out[dst] = tags[src_key]
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "codec" not in out:
            out["codec"] = stream.get("codec_name")
            out["width"] = stream.get("width")
            out["height"] = stream.get("height")
            rate = stream.get("avg_frame_rate") or "0/1"
            try:
                num, den = rate.split("/")
                if int(den):
                    out["fps"] = round(int(num) / int(den), 2)
            except ValueError:
                pass
    return out


# fields shown publicly on detail pages (GPS is handled separately — it can
# expose a reporter's home even when they asked to obscure the location)
PUBLIC_FIELDS = [
    ("make", "Device make"), ("model", "Device model"), ("lens", "Lens"),
    ("captured_at", "Captured at"), ("width", None), ("height", None),
    ("exposure", "Exposure"), ("f_number", "Aperture (f/)"), ("iso", "ISO"),
    ("focal_length_mm", "Focal length (mm)"), ("software", "Software"),
    ("codec", "Video codec"), ("fps", "Frame rate"),
    ("duration_s", "Duration (s)"), ("bitrate_kbps", "Bitrate (kbps)"),
    ("encoder", "Encoder"), ("format", "Container"),
]


def public_rows(meta: dict, *, include_gps: bool) -> list[tuple[str, str]]:
    rows = []
    if meta.get("width") and meta.get("height"):
        rows.append(("Resolution", f"{meta['width']}×{meta['height']}"))
    for key, label in PUBLIC_FIELDS:
        if label is None or key in ("width", "height"):
            continue
        if meta.get(key) not in (None, ""):
            rows.append((label, str(meta[key])))
    if include_gps and meta.get("gps_lat") is not None:
        rows.append(("GPS (from file)", f"{meta['gps_lat']:.3f}, {meta['gps_lon']:.3f}"))
    return rows
