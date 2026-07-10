import re
from app import r2


def test_make_upload_key_format():
    key = r2.make_upload_key("image/jpeg")
    assert re.fullmatch(r"uploads/\d{4}/\d{2}/[0-9a-f]{32}\.jpg", key)
    assert r2.make_upload_key("video/mp4").endswith(".mp4")
    assert r2.make_upload_key("video/quicktime").endswith(".mov")


def test_presign_put_is_signed_url_for_key():
    key = "uploads/2026/07/aabbccddeeff00112233445566778899.jpg"
    url = r2.presign_put(key, "image/jpeg", 1000)
    assert url.startswith("https://r2.test/test-bucket/uploads/")
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=900" in url


def test_public_url():
    key = "uploads/2026/07/aabbccddeeff00112233445566778899.jpg"
    assert r2.public_url(key) == f"https://media.test/{key}"


def test_allowed_types():
    assert r2.ALLOWED_IMAGE["image/png"] == ".png"
    assert r2.ALLOWED_VIDEO["video/mp4"] == ".mp4"
    assert "video/mp4" not in r2.ALLOWED_IMAGE
