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
from typing import Any, Dict, List, Optional, Union, get_args, get_origin, get_type_hints

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def parse_dotenv(text: str) -> Dict[str, str]:
    """Parse ``KEY=value`` lines. A repeated key takes its last value."""
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_dotenv(path: Path) -> None:
    """Load *path* into os.environ. The real environment still wins."""
    if not path.exists():
        return
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if key not in os.environ:
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
    # Instagram raised Reels from 90s to 3 minutes, and YouTube caps Shorts
    # at 3 minutes too, so this lets through anything Shorts can produce.
    max_duration: int = 180
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
    # Kept in step with filters.max_duration: anything longer was already
    # skipped at discovery, so the ffmpeg trim is only ever a safety net.
    max_duration: int = 180
    fps: int = 30
    crf: int = 23
    # x264 speed/size trade-off. `veryfast` is roughly 4x quicker than
    # `medium` for a few percent more bitrate — worth it on a Raspberry Pi.
    preset: str = "medium"
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
class ReviewConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    # Required before the UI may bind to anything but loopback: it shows your
    # queue and can approve posts, and it is otherwise wide open.
    password: str = ""


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
    review: ReviewConfig = field(default_factory=ReviewConfig)
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


TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}
#: Strings that mean "not set" — an unset ${VAR} expands to one of these.
NULL_WORDS = {"", "null", "none", "~"}


def coerce(value: Any, hint: Any, where: str) -> Any:
    """Convert *value* to the type a config field declares.

    Everything arriving from an environment variable is a string — that is how
    Docker and CasaOS pass settings — so `max_per_day: "3"` has to become an
    int before anything compares against it.
    """
    origin = get_origin(hint)

    # Optional[X] / Union[X, None]
    if origin is Union:
        args = [a for a in get_args(hint) if a is not type(None)]
        if isinstance(value, str) and value.strip().lower() in NULL_WORDS:
            return None
        if value is None:
            return None
        return coerce(value, args[0], where) if len(args) == 1 else value

    if value is None:
        return None

    if origin in (list, List):
        item_hint = (get_args(hint) or (str,))[0]
        if isinstance(value, str):
            # "9,13,19" and "#reels #shorts" both read naturally in a GUI field.
            parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
        elif isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            parts = [value]
        return [coerce(p, item_hint, where) for p in parts]

    if hint is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE_WORDS:
            return True
        if text in FALSE_WORDS:
            return False
        raise ConfigError(f"{where}: expected true or false, got {value!r}")

    if hint in (int, float) and not isinstance(value, bool):
        try:
            return hint(str(value).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: expected a number, got {value!r}") from None

    if hint is str and not isinstance(value, str):
        return str(value)

    return value


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
    hints = get_type_hints(cls)
    section = cls.__name__.replace("Config", "").lower()
    values = {
        k: coerce(v, hints.get(k, Any), f"{section}.{k}")
        for k, v in data.items()
        if k in known
    }
    return cls(**values)


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
        review=_build(ReviewConfig, raw.get("review")),
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
