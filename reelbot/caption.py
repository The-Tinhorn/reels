"""Build Instagram captions from video metadata."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from reelbot.config import CaptionConfig
from reelbot.store import Video

TOKEN = re.compile(r"\{([a-z_]+)\}")
HASHTAG = re.compile(r"#\w+")
#: Instagram silently drops a post's hashtags past this count.
MAX_HASHTAGS = 30


def render_template(template: str, values: Dict[str, str]) -> str:
    """Substitute ``{key}`` tokens, leaving unknown braces (and title braces) alone."""
    return TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def normalize_hashtags(tags: List[str]) -> List[str]:
    seen, out = set(), []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        tag = re.sub(r"[^#\w]", "", tag)
        key = tag.lower()
        if len(tag) > 1 and key not in seen:
            seen.add(key)
            out.append(tag)
    return out[:MAX_HASHTAGS]


def build_caption(video: Video, cfg: CaptionConfig, extra_hashtags: Optional[List[str]] = None) -> str:
    """Render the caption for *video*, honouring a reviewer's manual override."""
    if video.caption:
        return video.caption[: cfg.max_length]

    hashtags = normalize_hashtags(list(cfg.hashtags) + list(extra_hashtags or []))
    values = {
        "title": (video.title or "").strip(),
        "channel": (video.channel or "").strip(),
        "channel_url": video.channel_url or "",
        "url": video.url or "",
        "description": (video.description or "").strip(),
        "views": f"{video.view_count:,}" if video.view_count else "",
        "hashtags": " ".join(hashtags),
        "upload_date": video.upload_date or "",
    }

    caption = render_template(cfg.template, values)
    if cfg.append_source_url and video.url and video.url not in caption:
        caption = f"{caption}\n\n{video.url}"

    # Collapse the blank lines left behind by empty tokens.
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    return truncate(caption, cfg.max_length)


def truncate(text: str, limit: int) -> str:
    """Trim to *limit* characters without cutting a word or a hashtag in half."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > limit * 0.6:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def count_hashtags(caption: str) -> int:
    return len(HASHTAG.findall(caption))
