import pytest

from app import ytdetect

CANON = "https://www.youtube.com/watch?v=XHWPQEJ_TVA"


@pytest.mark.parametrize("text", [
    "https://www.youtube.com/watch?v=XHWPQEJ_TVA",
    "https://youtube.com/shorts/XHWPQEJ_TVA",
    "https://youtu.be/XHWPQEJ_TVA",
    "https://m.youtube.com/watch?v=XHWPQEJ_TVA&feature=share",
    "https://www.youtube.com/watch?app=desktop&v=XHWPQEJ_TVA",
    "https://music.youtube.com/watch?v=XHWPQEJ_TVA",
    "https://www.youtube.com/live/XHWPQEJ_TVA",
    "https://www.youtube.com/embed/XHWPQEJ_TVA",
    "Saw this last night: https://youtu.be/XHWPQEJ_TVA amazing footage",
])
def test_find_in_text_variants(text):
    assert ytdetect.find_in_text(text) == CANON


def test_first_match_wins():
    text = ("video https://youtu.be/XHWPQEJ_TVA and my channel promo "
            "https://youtu.be/aaaaaaaaaaa")
    assert ytdetect.find_in_text(text) == CANON


@pytest.mark.parametrize("text", [
    None, "", "no links here",
    "https://www.youtube.com/@somechannel",
    "https://www.youtube.com/channel/UCabcdefghij",
    "https://www.youtube.com/playlist?list=PLxyzabcdefghijk",
    "https://v.redd.it/abc123",
])
def test_find_in_text_non_matches(text):
    assert ytdetect.find_in_text(text) is None


def test_post_url_beats_selftext():
    post = {"url": "https://youtu.be/XHWPQEJ_TVA",
            "selftext": "also https://youtu.be/aaaaaaaaaaa"}
    assert ytdetect.find_youtube_url(post) == CANON


def test_post_body_link_only():
    # the Okinawa case (1upvfrb): self post, YouTube URL in the body
    post = {"url": "https://www.reddit.com/r/UFOs/comments/1upvfrb/x/",
            "selftext": "Footage here https://youtu.be/XHWPQEJ_TVA"}
    assert ytdetect.find_youtube_url(post) == CANON


def test_post_no_youtube():
    assert ytdetect.find_youtube_url({"url": "https://i.redd.it/a.jpg",
                                      "selftext": ""}) is None
