"""Media tests. The ones that need ffmpeg skip themselves when it is absent."""

import shutil
import subprocess
from pathlib import Path

import pytest

from reelbot import media
from reelbot.config import MediaConfig

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def make_test_video(path: Path, width=640, height=480, seconds=2, audio=True, codec="libx264"):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30:duration={seconds}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p", "-t", str(seconds), str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def test_build_filter_blur_and_black():
    cfg = MediaConfig(target_width=1080, target_height=1920, fps=30)
    blur = media.build_filter(cfg, "blur")
    assert "boxblur" in blur and "overlay" in blur and blur.endswith("[v]")
    black = media.build_filter(cfg, "black")
    assert "pad=1080:1920" in black and "boxblur" not in black


def test_parse_fps():
    assert media._parse_fps("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert media._parse_fps("25") == 25.0
    assert media._parse_fps("0/0") == 0.0
    assert media._parse_fps(None) == 0.0


def test_conforms_checks_every_requirement():
    cfg = MediaConfig()
    good = media.Probe(width=1080, height=1920, duration=30, video_codec="h264",
                       audio_codec="aac", pix_fmt="yuv420p", container="mov,mp4,m4a")
    assert media.conforms(good, cfg) is True

    for field, value in [
        ("video_codec", "vp9"), ("audio_codec", ""), ("pix_fmt", "yuv444p"),
        ("container", "matroska"), ("duration", 500), ("width", 1081),
    ]:
        bad = media.Probe(**{**good.__dict__, field: value})
        assert media.conforms(bad, cfg) is False, f"{field}={value} should not conform"


@needs_ffmpeg
def test_probe_reads_real_files(tmp_path):
    src = make_test_video(tmp_path / "in.mp4", width=640, height=480, seconds=2)
    info = media.probe(src, MediaConfig())
    assert (info.width, info.height) == (640, 480)
    assert info.video_codec == "h264" and info.has_audio
    assert info.duration == pytest.approx(2.0, abs=0.3)


@needs_ffmpeg
def test_probe_rejects_a_non_video(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"definitely not a video")
    with pytest.raises(media.MediaError):
        media.probe(junk, MediaConfig())


@needs_ffmpeg
def test_normalize_produces_a_reels_ready_file(tmp_path):
    cfg = MediaConfig(target_width=540, target_height=960, crf=30)  # small = fast
    src = make_test_video(tmp_path / "in.mp4", width=640, height=480, seconds=2)
    out = media.normalize(src, tmp_path / "out.mp4", cfg)

    info = media.probe(out, cfg)
    assert (info.width, info.height) == (540, 960)   # landscape source, vertical output
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.pix_fmt == "yuv420p"
    assert media.conforms(info, cfg) is True


@needs_ffmpeg
def test_normalize_adds_silent_audio_when_the_source_has_none(tmp_path):
    cfg = MediaConfig(target_width=540, target_height=960, crf=30)
    src = make_test_video(tmp_path / "silent.mp4", seconds=2, audio=False)
    assert media.probe(src, cfg).has_audio is False

    out = media.normalize(src, tmp_path / "out.mp4", cfg)
    assert media.probe(out, cfg).has_audio is True


@needs_ffmpeg
def test_normalize_trims_to_max_duration(tmp_path):
    cfg = MediaConfig(target_width=360, target_height=640, crf=32, max_duration=2)
    src = make_test_video(tmp_path / "long.mp4", width=360, height=640, seconds=5)
    out = media.normalize(src, tmp_path / "out.mp4", cfg)
    assert media.probe(out, cfg).duration <= 2.5


@needs_ffmpeg
def test_prepare_skips_re_encoding_a_conforming_file(tmp_path, cfg):
    cfg.media = MediaConfig(target_width=540, target_height=960, crf=30)
    src = make_test_video(tmp_path / "in.mp4", width=640, height=480, seconds=2)
    normalized = media.normalize(src, tmp_path / "ready.mp4", cfg.media)

    out = media.prepare(normalized, cfg)
    assert out == normalized                        # already fine, left alone
    assert not (tmp_path / "ready.reel.mp4").exists()


@needs_ffmpeg
def test_prepare_normalizes_a_non_conforming_file(tmp_path, cfg):
    cfg.media = MediaConfig(target_width=540, target_height=960, crf=30)
    src = make_test_video(tmp_path / "in.mp4", width=640, height=480, seconds=2)
    out = media.prepare(src, cfg)
    assert out == tmp_path / "in.reel.mp4" and out.exists()
    assert media.conforms(media.probe(out, cfg.media), cfg.media)


def test_prepare_is_a_no_op_when_normalization_is_off(tmp_path, cfg):
    cfg.media.normalize = False
    src = tmp_path / "whatever.mp4"
    src.write_bytes(b"x")
    assert media.prepare(src, cfg) == src


@needs_ffmpeg
def test_cover_frame(tmp_path):
    cfg = MediaConfig()
    src = make_test_video(tmp_path / "in.mp4", seconds=2)
    out = media.cover_frame(src, tmp_path / "cover.jpg", cfg, at=0.5)
    assert out and out.exists() and out.stat().st_size > 0


@needs_ffmpeg
def test_normalize_warns_when_it_actually_cuts_the_video(tmp_path, caplog):
    """Truncating a post silently is worse than a loud warning."""
    cfg = MediaConfig(target_width=360, target_height=640, crf=32, max_duration=2)
    src = make_test_video(tmp_path / "long.mp4", width=360, height=640, seconds=5)

    with caplog.at_level("WARNING"):
        media.normalize(src, tmp_path / "out.mp4", cfg)
    assert "will be cut to 2s" in caplog.text


@needs_ffmpeg
def test_normalize_is_quiet_when_nothing_is_cut(tmp_path, caplog):
    cfg = MediaConfig(target_width=360, target_height=640, crf=32, max_duration=180)
    src = make_test_video(tmp_path / "short.mp4", width=360, height=640, seconds=2)

    with caplog.at_level("WARNING"):
        media.normalize(src, tmp_path / "out.mp4", cfg)
    assert "will be cut" not in caplog.text


def test_the_two_max_duration_defaults_match():
    """A filter that lets a video through must not then be trimmed by ffmpeg."""
    from reelbot.config import Filters, MediaConfig as MC

    assert Filters().max_duration == MC().max_duration
