"""Configuration loading.

Config lives in a YAML file. Any ``${VAR}`` or ``${VAR:-default}`` token in a
string value is expanded from the environment (and from a ``.env`` file sitting
next to the config), so secrets never have to be written into the YAML itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv(path: Path) -> None:
    """Load ``KEY=value`` pairs from *path* into os.environ without overriding."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` tokens in strings inside dicts/lists."""
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default if default is not None else "")

        return ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


@dataclass
class Source:
    """One place to look for candidate Shorts."""

    name: str
    url: str
    limit: int = 20
    # Only set this for channels whose content you are allowed to repost
    # (normally: your own). Everything else goes through human review.
    auto_approve: bool = False


@dataclass
class Filters:
    max_duration: int = 90          # Reels hard limit is 90s for most accounts
    min_duration: int = 3
    min_views: int = 0
    published_within_days: Optional[int] = None
    title_allow: List[str] = field(default_factory=list)
    title_deny: List[str] = field(default_factory=list)


@dataclass
class DownloadConfig:
    dir: str = "data/downloads"
    # Prefer a vertical MP4/H.264 stream; yt-dlp merges A/V when needed.
    format: str = "bv*[ext=mp4][height<=1920]+ba[ext=m4a]/bv*+ba/b"
    cookies_file: Optional[str] = None
    cookies_from_browser: Optional[str] = None
    rate_limit: Optional[str] = None
    retries: int = 3
    write_thumbnail: bool = True


@dataclass
class MediaConfig:
    normalize: bool = True
    target_width: int = 1080
    target_height: int = 1920
    max_duration: int = 90
    fps: int = 30
    crf: int = 23
    # How to fill the frame when the source is not already 9:16.
    background: str = "blur"   # blur | black
    audio_bitrate: str = "128k"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


@dataclass
class CaptionConfig:
    template: str = "{title}\n\n\U0001f3a5 via {channel}\n{hashtags}"
    hashtags: List[str] = field(default_factory=lambda: ["#reels", "#shorts"])
    max_length: int = 2200
    append_source_url: bool = False


@dataclass
class PublishConfig:
    backend: str = "dryrun"          # dryrun | instagrapi | graph
    max_per_day: Optional[int] = 3   # None = unlimited; 0 pauses posting
    min_minutes_between_posts: int = 90
    posting_hours: List[int] = field(default_factory=list)  # empty = any hour
    share_to_feed: bool = True
    max_attempts: int = 3
    delete_after_post: bool = False


@dataclass
class InstagramConfig:
    username: str = ""
    password: str = ""
    verification_code: str = ""      # static 2FA seed code, if you use one
    session_file: str = "data/ig_session.json"
    proxy: Optional[str] = None
    # Random jitter (seconds) applied around API calls to look less robotic.
    delay_range: List[int] = field(default_factory=lambda: [2, 8])


@dataclass
class GraphConfig:
    """Optional official Meta Graph API backend (requires API keys)."""

    ig_user_id: str = ""
    access_token: str = ""
    # Reels must be fetched by Meta from a public URL, so the normalized file
    # has to be reachable at {public_base_url}/{filename}.
    public_base_url: str = ""
    poll_seconds: int = 5
    poll_attempts: int = 60


@dataclass
class Config:
    db_path: str = "data/reelbot.db"
    sources: List[Source] = field(default_factory=list)
    filters: Filters = field(default_factory=Filters)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    caption: CaptionConfig = field(default_factory=CaptionConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    instagram: InstagramConfig = field(default_factory=InstagramConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    path: Optional[str] = None

    @property
    def root(self) -> Path:
        """Directory that relative paths in the config resolve against."""
        return Path(self.path).resolve().parent if self.path else Path.cwd()

    def resolve(self, relative: str) -> Path:
        p = Path(relative).expanduser()
        return p if p.is_absolute() else (self.root / p)


def _build(cls, data: Optional[Dict[str, Any]]):
    """Instantiate dataclass *cls* from *data*, ignoring unknown keys."""
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"expected a mapping for {cls.__name__}, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"unknown option(s) for {cls.__name__.lower()}: {', '.join(sorted(unknown))}"
        )
    return cls(**{k: v for k, v in data.items() if k in known})


class ConfigError(Exception):
    pass


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"config file not found: {path} (run `reelbot init` to create one)")

    load_dotenv(path.resolve().parent / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    raw = expand_env(raw)

    sources = [_build(Source, s) for s in raw.get("sources") or []]
    for s in sources:
        if not s.url:
            raise ConfigError(f"source {s.name!r} has no url")

    cfg = Config(
        db_path=raw.get("db_path", "data/reelbot.db"),
        sources=sources,
        filters=_build(Filters, raw.get("filters")),
        download=_build(DownloadConfig, raw.get("download")),
        media=_build(MediaConfig, raw.get("media")),
        caption=_build(CaptionConfig, raw.get("caption")),
        publish=_build(PublishConfig, raw.get("publish")),
        instagram=_build(InstagramConfig, raw.get("instagram")),
        graph=_build(GraphConfig, raw.get("graph")),
        path=str(path.resolve()),
    )

    if cfg.publish.backend not in {"dryrun", "instagrapi", "graph"}:
        raise ConfigError(
            f"publish.backend must be one of dryrun/instagrapi/graph, got {cfg.publish.backend!r}"
        )
    return cfg


def as_dict(obj: Any) -> Any:
    """dataclasses.asdict that tolerates plain values (used for `status` output)."""
    if is_dataclass(obj):
        return {f.name: as_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [as_dict(v) for v in obj]
    return obj
