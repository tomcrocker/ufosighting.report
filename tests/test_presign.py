GOOD = {"filename": "orb.jpg", "content_type": "image/jpeg", "size_bytes": 5_000_000}


def test_presign_requires_login(client):
    assert client.post("/api/presign", json=GOOD).status_code == 401


def test_presign_success_image(logged_in):
    r = logged_in.post("/api/presign", json=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "image"
    assert body["key"].startswith("uploads/") and body["key"].endswith(".jpg")
    assert "X-Amz-Signature=" in body["upload_url"]
    assert body["public_url"] == f"https://media.test/{body['key']}"


def test_presign_success_video(logged_in):
    r = logged_in.post(
        "/api/presign",
        json={"filename": "ufo.mp4", "content_type": "video/mp4", "size_bytes": 100_000_000},
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "video"


def test_presign_rejects_unsupported_type(logged_in):
    r = logged_in.post(
        "/api/presign",
        json={"filename": "x.exe", "content_type": "application/octet-stream", "size_bytes": 100},
    )
    assert r.status_code == 400


def test_presign_rejects_oversize(logged_in):
    r = logged_in.post(
        "/api/presign",
        json={"filename": "big.jpg", "content_type": "image/jpeg", "size_bytes": 26 * 1024 * 1024},
    )
    assert r.status_code == 400
    r = logged_in.post(
        "/api/presign",
        json={"filename": "big.mp4", "content_type": "video/mp4", "size_bytes": 501 * 1024 * 1024},
    )
    assert r.status_code == 400
