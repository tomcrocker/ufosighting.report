from app.mdrender import reddit_md


def test_bold_renders():
    assert "<strong>orb</strong>" in reddit_md("a **orb** appeared")


def test_link_renders():
    html = reddit_md("[clip](https://youtu.be/XHWPQEJ_TVA)")
    assert '<a href="https://youtu.be/XHWPQEJ_TVA"' in html and ">clip</a>" in html


def test_bare_url_linkified():
    assert '<a href="https://example.com/v.mp4"' in reddit_md("see https://example.com/v.mp4 now")


def test_subreddit_and_user_mentions_linkified():
    html = reddit_md("posted in r/UFOs by u/tmosh")
    assert '<a href="https://www.reddit.com/r/UFOs"' in html
    assert '<a href="https://www.reddit.com/u/tmosh"' in html


def test_html_is_escaped_not_executed():
    html = reddit_md('<script>alert(1)</script>')
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_single_newlines_become_breaks():
    html = reddit_md("line one\nline two")
    assert "<br" in html


def test_zero_width_and_entities_cleaned():
    html = reddit_md("before\n\n&amp;#x200B;\n\nafter")
    assert "#x200B" not in html and "​" not in html


def test_empty_and_none_safe():
    assert reddit_md("") == ""
    assert reddit_md(None) == ""
