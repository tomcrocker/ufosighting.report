"""Display-time markdown rendering for Reddit-sourced text (the ufosarchive
reddit_md pattern): descriptions are stored as raw Reddit markdown, so bold/
links/etc. must render at template time. escape=True keeps archived content
XSS-safe; hard_wrap preserves the single-newline line breaks Reddit shows."""
import html
import re
from functools import lru_cache

import mistune

_md = mistune.create_markdown(escape=True, hard_wrap=True,
                              plugins=["url", "strikethrough", "table"])
_ZERO_WIDTH = re.compile("[​‌‍﻿]")
# r/sub and u/user mentions not already part of a URL or markdown link
_MENTION = re.compile(r"(?<![\w/])/?([ru])/([A-Za-z0-9_-]{2,21})")


def _linkify_mentions(text: str) -> str:
    return _MENTION.sub(r"[\g<0>](https://www.reddit.com/\1/\2)", text)


@lru_cache(maxsize=4096)
def reddit_md(text: str | None) -> str:
    if not text:
        return ""
    # API/dump text can arrive HTML-escaped (sometimes doubly: &amp;#x200B;)
    cleaned = html.unescape(html.unescape(text))
    cleaned = _ZERO_WIDTH.sub("", cleaned)
    return _md(_linkify_mentions(cleaned))
