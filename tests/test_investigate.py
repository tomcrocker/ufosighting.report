from pathlib import Path

from app.investigate_data import ENTRIES

IMAGE_DIR = Path(__file__).resolve().parent.parent / "static" / "investigate"


def test_investigate_renders_entries(client):
    r = client.get("/investigate")
    assert r.status_code == 200
    assert "Investigate a sighting" in r.text
    for title in ("Starlink", "Sky Lanterns", "Lenticular clouds"):
        assert title in r.text
    assert "https://ufos.wiki/investigation/starlink/" in r.text
    assert "https://ufos.wiki/investigate/" in r.text  # source credit


def test_investigate_category_chips(client):
    r = client.get("/investigate")
    assert 'data-cat=""' in r.text  # the All chip
    for cat in ("Artifacts", "Astronomic", "Atmospheric", "Common",
                "Man-made", "Nature", "Optical Effects"):
        assert f'data-cat="{cat}"' in r.text


def test_investigate_resources_strip(client):
    r = client.get("/investigate")
    for url in ("https://www.flightradar24.com", "https://globe.adsbexchange.com",
                "https://stellarium-web.org", "https://www.heavens-above.com"):
        assert url in r.text


def test_investigate_images_exist_on_disk(client):
    html = client.get("/investigate").text
    with_image = [e for e in ENTRIES if e["image"]]
    assert with_image, "expected entries with matched images"
    for e in with_image[:5] + with_image[-5:]:  # spot-check both ends
        assert f'/static/investigate/{e["image"]}' in html
        assert (IMAGE_DIR / e["image"]).is_file()


def test_investigate_data_shape():
    assert len(ENTRIES) >= 50
    for e in ENTRIES:
        assert e["title"] and e["slug"] and e["categories"]
        assert e["source_url"].startswith("https://ufos.wiki/investigation/")
        assert not e["teaser"].endswith("...")  # truncation ellipses stripped
