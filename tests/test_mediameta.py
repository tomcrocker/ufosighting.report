import io
import json
import subprocess

from PIL import ExifTags, Image

from app import mediameta


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (120, 80), "black")
    exif = Image.Exif()
    exif[0x010F] = "Apple"            # Make
    exif[0x0110] = "iPhone 16 Pro"    # Model
    exif[0x0131] = "18.1"             # Software
    ifd = {
        0x9003: "2026:07:10 22:15:00",   # DateTimeOriginal
        0x829A: (1, 25),                 # ExposureTime 1/25
        0x829D: 1.8,                     # FNumber
        0x8827: 640,                     # ISO
        0x920A: 6.86,                    # FocalLength
    }
    for k, v in ifd.items():
        exif[k] = v
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_extract_image_meta_curated_fields():
    meta = mediameta.extract_image_meta(_jpeg_with_exif())
    assert meta["make"] == "Apple" and meta["model"] == "iPhone 16 Pro"
    assert meta["width"] == 120 and meta["format"] == "JPEG"
    assert meta["captured_at"] == "2026:07:10 22:15:00"


def test_extract_image_meta_garbage_is_empty():
    assert mediameta.extract_image_meta(b"not an image") == {}


def test_extract_video_meta_parses_ffprobe(monkeypatch):
    probe = {
        "format": {"duration": "26.44", "bit_rate": "3500000",
                   "tags": {"creation_time": "2026-07-10T22:15:00Z",
                            "com.apple.quicktime.model": "iPhone 16 Pro"}},
        "streams": [{"codec_type": "video", "codec_name": "hevc",
                     "width": 3840, "height": 2160, "avg_frame_rate": "60/1"}],
    }

    class FakeProc:
        returncode = 0
        stdout = json.dumps(probe).encode()

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    meta = mediameta.extract_video_meta("https://media.test/v.mp4")
    assert meta["codec"] == "hevc" and meta["width"] == 3840
    assert meta["fps"] == 60.0 and meta["duration_s"] == 26.44
    assert meta["model"] == "iPhone 16 Pro"
    assert meta["captured_at"] == "2026-07-10T22:15:00Z"


def test_public_rows_gps_gate():
    meta = {"make": "Apple", "width": 100, "height": 50,
            "gps_lat": 48.123456, "gps_lon": -123.654321}
    with_gps = dict(mediameta.public_rows(meta, include_gps=True))
    without = dict(mediameta.public_rows(meta, include_gps=False))
    assert "GPS (from file)" in with_gps
    assert with_gps["GPS (from file)"] == "48.123, -123.654"  # rounded ~100m
    assert "GPS (from file)" not in without
    assert with_gps["Resolution"] == "100×50"


def test_extract_video_meta_gopro_and_android(monkeypatch):
    probe = {
        "format": {"duration": "12.0",
                   "tags": {"firmware": "HD9.01.01.60.00",
                            "location": "+48.4284-123.3656+030.000/"}},
        "streams": [{"codec_type": "video", "codec_name": "h264",
                     "width": 1920, "height": 1080, "avg_frame_rate": "30/1",
                     "tags": {"handler_name": "GoPro AVC"}}],
    }

    class FakeProc:
        returncode = 0
        stdout = json.dumps(probe).encode()

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    meta = mediameta.extract_video_meta("https://media.test/g.mp4")
    assert meta["make"] == "GoPro"
    assert meta["software"] == "HD9.01.01.60.00"
    assert meta["gps_lat"] == 48.4284 and meta["gps_lon"] == -123.3656


def test_extract_video_meta_android_version(monkeypatch):
    probe = {
        "format": {"tags": {"com.android.version": "15",
                            "com.android.manufacturer": "Google",
                            "com.android.model": "Pixel 9 Pro"}},
        "streams": [{"codec_type": "video", "codec_name": "hevc",
                     "width": 3840, "height": 2160, "avg_frame_rate": "30/1"}],
    }

    class FakeProc:
        returncode = 0
        stdout = json.dumps(probe).encode()

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    meta = mediameta.extract_video_meta("https://media.test/a.mp4")
    assert meta["software"] == "Android 15"
    assert meta["make"] == "Google" and meta["model"] == "Pixel 9 Pro"


def test_extract_compass_and_altitude():
    img = Image.new("RGB", (60, 40), "black")
    exif = Image.Exif()
    gps_ifd = {1: "N", 2: (48.0, 25.0, 42.24), 3: "W", 4: (123.0, 21.0, 56.16),
               5: 0, 6: 31.5, 16: "T", 17: 227.4}
    exif[ExifTags.IFD.GPSInfo] = gps_ifd
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    meta = mediameta.extract_image_meta(out.getvalue())
    assert meta["gps_altitude_m"] == 31.5
    assert meta["compass_deg"] == 227.4 and meta["compass_ref"] == "true"
    rows = dict(mediameta.public_rows(meta, include_gps=True))
    assert rows["Camera heading"] == "227.4° (true north)"


def test_video_audio_and_hdr(monkeypatch):
    probe = {
        "format": {"tags": {}},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840,
             "height": 2160, "avg_frame_rate": "30/1", "color_transfer": "smpte2084"},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2,
             "sample_rate": "48000"},
        ],
    }

    class FakeProc:
        returncode = 0
        stdout = json.dumps(probe).encode()

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    meta = mediameta.extract_video_meta("https://media.test/h.mp4")
    assert meta["hdr"] == "HDR (PQ)"
    assert meta["audio"] == "aac 2ch 48000Hz"


def test_video_silent_flagged(monkeypatch):
    probe = {"format": {"tags": {}},
             "streams": [{"codec_type": "video", "codec_name": "h264",
                          "width": 1920, "height": 1080, "avg_frame_rate": "30/1"}]}

    class FakeProc:
        returncode = 0
        stdout = json.dumps(probe).encode()

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    assert mediameta.extract_video_meta("u")["audio"] == "none (silent file)"


def test_provenance_original_camera_image():
    p = mediameta.provenance({"make": "Apple", "model": "iPhone 15", "iso": 100,
                              "f_number": 1.8, "format": "JPEG"})
    assert p["original"] is True and "iPhone 15" in p["detail"]


def test_provenance_original_video():
    p = mediameta.provenance({"make": "Apple", "model": "iPhone 15", "codec": "hevc"})
    assert p["original"] is True


def test_provenance_screenshot():
    p = mediameta.provenance({"software": "Monosnap", "format": "PNG"})
    assert p["original"] is False and p["label"] == "Screenshot"


def test_provenance_editor_overrides_camera():
    p = mediameta.provenance({"make": "Apple", "model": "iPhone 15",
                              "software": "Adobe Photoshop 2026", "iso": 100})
    assert p["original"] is False and "Edited" in p["label"]


def test_provenance_no_metadata():
    assert mediameta.provenance({})["label"] == "No metadata"


def test_provenance_stripped_no_camera():
    p = mediameta.provenance({"width": 800, "height": 600, "format": "PNG"})
    assert p["original"] is False and "No camera" in p["label"]


def test_provenance_ios_version_not_flagged():
    # iOS stamps its version in Software; must NOT read as editing software
    p = mediameta.provenance({"make": "Apple", "model": "iPhone 15",
                              "software": "17.5.1", "iso": 100})
    assert p["original"] is True
