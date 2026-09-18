"""Download approved videos with yt-dlp."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from reelbot.config import Config
from reelbot.store import APPROVED, DOWNLOADED, FAILED, Store, Video, utcnow
from reelbot.ytdlp import COOKIE_HELP, apply_auth, looks_like_bot_check

log = logging.getLogger(__name__)

#: Signature of a downloader: (url, yt-dlp opts) -> sanitized info dict.
Downloader = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class DownloadError(Exception):
    pass


def build_opts(cfg: Config, outdir: Path) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "format": cfg.download.format,
        "merge_output_format": "mp4",
        "retries": cfg.download.retries,
        "fragment_retries": cfg.download.retries,
        "concurrent_fragment_downloads": 4,
        "noplaylist": True,
        "writethumbnail": cfg.download.write_thumbnail,
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        ],
    }
    if cfg.download.rate_limit:
        opts["ratelimit"] = _parse_rate(cfg.download.rate_limit)
    return apply_auth(cfg, opts)


def _parse_rate(value: str) -> Optional[int]:
    """Parse yt-dlp style rate limits ('500K', '1.5M') into bytes/second."""
    text = str(value).strip().upper()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    multiplier = 1
    if text and text[-1] in multipliers:
        multiplier = multipliers[text[-1]]
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        log.warning("could not parse download.rate_limit=%r, ignoring", value)
        return None


def _ytdlp_download(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    from yt_dlp import YoutubeDL  # lazy import keeps the unit tests dependency-free

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DownloadError("yt-dlp returned no info")
        return ydl.sanitize_info(info)


def _find_media(outdir: Path, video_id: str) -> Optional[Path]:
    for ext in ("mp4", "mkv", "webm", "mov"):
        candidate = outdir / f"{video_id}.{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    matches = sorted(
        (p for p in outdir.glob(f"{video_id}.*") if p.suffix.lower() not in {".json", ".part"}),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return matches[0] if matches else None


def _find_thumb(outdir: Path, video_id: str) -> Optional[Path]:
    for ext in ("jpg", "jpeg", "png", "webp"):
        candidate = outdir / f"{video_id}.{ext}"
        if candidate.exists():
            return candidate
    return None


def download_video(
    cfg: Config,
    store: Store,
    video: Video,
    downloader: Optional[Downloader] = None,
) -> Video:
    """Download one approved video and mark it ``downloaded``.

    Refuses anything that has not been approved — this is the safety gate that
    keeps unreviewed material out of the publishing path.
    """
    if video.status != APPROVED:
        raise DownloadError(f"{video.id} is {video.status}, expected {APPROVED}")

    outdir = cfg.resolve(cfg.download.dir)
    outdir.mkdir(parents=True, exist_ok=True)

    downloader = downloader or _ytdlp_download
    log.info("downloading %s — %s", video.id, video.title[:60])
    try:
        info = downloader(video.url, build_opts(cfg, outdir))
    except Exception as exc:
        if looks_like_bot_check(exc):
            log.error("%s: %s", video.id, COOKIE_HELP)
        store.set_status(
            video.id, FAILED, error=f"download: {exc}", attempts=video.attempts + 1
        )
        raise DownloadError(str(exc)) from exc

    path = _find_media(outdir, video.id)
    if path is None:
        store.set_status(
            video.id, FAILED, error="download: no output file", attempts=video.attempts + 1
        )
        raise DownloadError(f"no output file for {video.id} in {outdir}")

    thumb = _find_thumb(outdir, video.id)
    fields: Dict[str, Any] = {
        "video_path": str(path),
        "thumb_path": str(thumb) if thumb else None,
        "downloaded_at": utcnow(),
        "error": None,
    }
    # Flat discovery often lacks these; the download response has the real values.
    if info.get("duration"):
        fields["duration"] = int(info["duration"])
    if info.get("view_count"):
        fields["view_count"] = int(info["view_count"])
    if info.get("title") and not video.title:
        fields["title"] = info["title"]
    if info.get("description") and not video.description:
        fields["description"] = info["description"][:5000]

    store.set_status(video.id, DOWNLOADED, **fields)
    log.info("downloaded %s -> %s", video.id, path)
    updated = store.get(video.id)
    assert updated is not None
    return updated


def download_approved(
    cfg: Config,
    store: Store,
    limit: Optional[int] = None,
    downloader: Optional[Downloader] = None,
) -> List[Video]:
    """Download every approved video (up to *limit*). Failures are recorded, not raised."""
    done: List[Video] = []
    for video in store.list(status=APPROVED, limit=limit):
        try:
            done.append(download_video(cfg, store, video, downloader=downloader))
        except DownloadError as exc:
            log.error("download failed for %s: %s", video.id, exc)
    return done
