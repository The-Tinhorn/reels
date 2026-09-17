"""Normalize downloaded videos to something Instagram reliably accepts as a Reel.

Instagram is picky: it wants an MP4 with H.264 video and an AAC audio track,
yuv420p pixels, a sane frame rate and a 9:16-ish frame. Shorts are usually
already close, so files that already conform are passed through untouched.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from reelbot.config import Config, MediaConfig

log = logging.getLogger(__name__)

#: Instagram rejects Reels outside roughly this aspect range.
MIN_ASPECT = 0.01
MAX_ASPECT = 10.0
#: How far from the target aspect ratio a file may be and still pass through.
ASPECT_TOLERANCE = 0.02


class MediaError(Exception):
    pass


@dataclass
class Probe:
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    pix_fmt: str = ""
    container: str = ""

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


def _run(cmd: List[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def have_ffmpeg(cfg: MediaConfig) -> bool:
    return bool(shutil.which(cfg.ffmpeg_path) and shutil.which(cfg.ffprobe_path))


def _parse_fps(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path, cfg: MediaConfig) -> Probe:
    """Read stream properties with ffprobe."""
    result = _run(
        [
            cfg.ffprobe_path, "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        timeout=120,
    )
    if result.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {result.stderr.strip()[:300]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"could not parse ffprobe output for {path}") from exc

    info = Probe(container=(data.get("format", {}).get("format_name") or ""))
    try:
        info.duration = float(data.get("format", {}).get("duration") or 0.0)
    except ValueError:
        info.duration = 0.0

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info.video_codec:
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.video_codec = stream.get("codec_name") or ""
            info.pix_fmt = stream.get("pix_fmt") or ""
            info.fps = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
        elif stream.get("codec_type") == "audio" and not info.audio_codec:
            info.audio_codec = stream.get("codec_name") or ""

    if not info.video_codec:
        raise MediaError(f"{path} has no video stream")
    return info


def conforms(info: Probe, cfg: MediaConfig) -> bool:
    """True when the file can be uploaded as-is, with no re-encode."""
    target_aspect = cfg.target_width / cfg.target_height
    return (
        info.video_codec == "h264"
        and info.has_audio
        and info.audio_codec == "aac"
        and info.pix_fmt == "yuv420p"
        and "mp4" in (info.container or "")
        and info.width % 2 == 0
        and info.height % 2 == 0
        and abs(info.aspect - target_aspect) <= ASPECT_TOLERANCE
        and (not cfg.max_duration or info.duration <= cfg.max_duration + 0.5)
        and MIN_ASPECT <= info.aspect <= MAX_ASPECT
    )


def build_filter(cfg: MediaConfig, background: str = "blur") -> str:
    """Video filter chain that fits any source into the target frame."""
    w, h = cfg.target_width, cfg.target_height
    common = f"setsar=1,fps={cfg.fps},format=yuv420p"
    if background == "blur":
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=luma_radius=30:luma_power=1[bg];"
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{common}[v]"
        )
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,{common}[v]"
    )


def normalize(
    src: Path,
    dest: Path,
    cfg: MediaConfig,
    background: str = "blur",
    info: Optional[Probe] = None,
) -> Path:
    """Re-encode *src* into *dest* as a Reels-ready MP4. Returns the written path."""
    if not have_ffmpeg(cfg):
        raise MediaError(
            f"{cfg.ffmpeg_path}/{cfg.ffprobe_path} not found on PATH — "
            "install ffmpeg or set media.normalize: false"
        )

    info = info or probe(src, cfg)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = [cfg.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if not info.has_audio:
        # Reels without an audio track are rejected; splice in silence.
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    cmd += ["-filter_complex", build_filter(cfg, background), "-map", "[v]"]
    cmd += ["-map", "1:a" if not info.has_audio else "0:a:0"]
    cmd += [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(cfg.crf),
        "-profile:v", "high",
        "-level", "4.1",
        "-c:a", "aac",
        "-b:a", cfg.audio_bitrate,
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
    ]
    if cfg.max_duration:
        cmd += ["-t", str(cfg.max_duration)]
    if not info.has_audio:
        cmd += ["-shortest"]
    cmd += [str(dest)]

    result = _run(cmd)
    if result.returncode != 0 or not dest.exists():
        raise MediaError(f"ffmpeg failed for {src}: {result.stderr.strip()[-500:]}")

    log.info("normalized %s -> %s", src.name, dest.name)
    return dest


def prepare(video_path: Path, cfg: Config, background: Optional[str] = None) -> Path:
    """Return a path that is safe to upload, normalizing only when needed."""
    media = cfg.media
    background = background or media.background
    if not media.normalize:
        return video_path
    if not have_ffmpeg(media):
        log.warning("ffmpeg not found — uploading %s as-is", video_path.name)
        return video_path

    try:
        info = probe(video_path, media)
    except MediaError as exc:
        log.warning("probe failed (%s) — uploading as-is", exc)
        return video_path

    if conforms(info, media):
        log.info("%s already conforms, skipping re-encode", video_path.name)
        return video_path

    dest = video_path.with_name(f"{video_path.stem}.reel.mp4")
    if dest.exists() and dest.stat().st_mtime >= video_path.stat().st_mtime:
        log.info("reusing existing normalized file %s", dest.name)
        return dest
    return normalize(video_path, dest, media, background=background, info=info)


def cover_frame(video_path: Path, dest: Path, cfg: MediaConfig, at: float = 0.5) -> Optional[Path]:
    """Grab a JPEG cover frame *at* seconds in (used as the Reel thumbnail)."""
    if not have_ffmpeg(cfg):
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            cfg.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(at), "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2", str(dest),
        ],
        timeout=120,
    )
    return dest if result.returncode == 0 and dest.exists() else None


def media_summary(path: Path, cfg: MediaConfig) -> Dict[str, Any]:
    info = probe(path, cfg)
    return {
        "resolution": f"{info.width}x{info.height}",
        "duration": round(info.duration, 2),
        "fps": round(info.fps, 2),
        "video_codec": info.video_codec,
        "audio_codec": info.audio_codec or "none",
        "pix_fmt": info.pix_fmt,
    }
