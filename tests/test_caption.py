from conftest import make_video
from reelbot.caption import build_caption, normalize_hashtags, render_template, truncate
from reelbot.config import CaptionConfig


def test_tokens_are_substituted():
    cfg = CaptionConfig(template="{title} by {channel}\n{hashtags}", hashtags=["reels"])
    caption = build_caption(make_video(), cfg)
    assert caption == "A test short by Test Channel\n#reels"


def test_braces_in_the_title_do_not_break_rendering():
    cfg = CaptionConfig(template="{title}", hashtags=[])
    caption = build_caption(make_video(title="cost {50%} off {unknown}"), cfg)
    assert caption == "cost {50%} off {unknown}"


def test_unknown_token_is_left_alone():
    assert render_template("hi {nope}", {"title": "x"}) == "hi {nope}"


def test_manual_override_wins():
    cfg = CaptionConfig(template="{title}", hashtags=["x"])
    assert build_caption(make_video(caption="my own words"), cfg) == "my own words"


def test_empty_tokens_collapse_blank_lines():
    cfg = CaptionConfig(template="{title}\n\n{description}\n\n{hashtags}", hashtags=["a"])
    caption = build_caption(make_video(description=""), cfg)
    assert "\n\n\n" not in caption
    assert caption == "A test short\n\n#a"


def test_source_url_appended_when_configured():
    cfg = CaptionConfig(template="{title}", hashtags=[], append_source_url=True)
    caption = build_caption(make_video(), cfg)
    assert caption.endswith("https://www.youtube.com/watch?v=abc123")


def test_hashtag_normalization():
    tags = normalize_hashtags(["reels", "#Reels", " #fun ", "bad tag!", ""])
    assert tags == ["#reels", "#fun", "#badtag"]


def test_hashtag_cap_is_thirty():
    assert len(normalize_hashtags([f"tag{i}" for i in range(50)])) == 30


def test_truncate_keeps_whole_words_and_respects_the_limit():
    text = "word " * 100
    out = truncate(text, 50)
    assert len(out) <= 50 and out.endswith("…")


def test_caption_respects_max_length():
    cfg = CaptionConfig(template="{title}", hashtags=[], max_length=20)
    assert len(build_caption(make_video(title="x" * 500), cfg)) <= 20
