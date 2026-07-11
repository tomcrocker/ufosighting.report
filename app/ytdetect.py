"""Detect YouTube URLs in Reddit posts — link posts and selftext bodies.

Only the first URL counts: sighting posts lead with their video; later
links are typically channel promo. Channel/user/playlist URLs never match
because the pattern requires a video-path form (watch/shorts/live/embed/v)
or youtu.be."""
import re

_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube(?:-nocookie)?\.com/"
    r"(?:watch\?(?:[^\s()\[\]]*&)?v=|shorts/|live/|embed/|v/)"
    r"|youtu\.be/)"
    rf"({_VIDEO_ID})"
)


def find_in_text(text: str | None) -> str | None:
    """First YouTube video URL in free text, canonicalised, or None."""
    if not text:
        return None
    m = _PATTERN.search(text)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def find_youtube_url(post: dict) -> str | None:
    """YouTube URL for a Reddit post: link-post URL first, then selftext."""
    return find_in_text(post.get("url")) or find_in_text(post.get("selftext"))
