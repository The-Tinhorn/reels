from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import make_video
from reelbot import pipeline
from reelbot.download import DownloadError, download_approved, download_video
from reelbot.publishers.base import PublishError, PublishResult
from reelbot.store import DOWNLOADED, FAILED, PENDING, POSTED


class FakePublisher:
    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def check(self):
        pass

    def publish(self, video_path, caption, thumbnail=None):
        if self.fail:
            raise PublishError("nope")
        self.calls.append((Path(video_path), caption))
        return PublishResult(media_id="123", permalink="https://instagram.com/reel/abc/",
                             backend=self.name)


@pytest.fixture
def downloaded(cfg, store, tmp_path):
    """A video that is approved, downloaded and sitting on disk."""
    store.add_candidate(make_video("v1"))
    store.approve("v1", by="tester")
    path = cfg.resolve(cfg.download.dir) / "v1.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really a video")
    store.set_status("v1", DOWNLOADED, video_path=str(path))
    cfg.media.normalize = False   # no ffmpeg in this unit test
    return store.get("v1")


# ----------------------------------------------------------- approval gating


def test_download_refuses_unapproved_videos(cfg, store):
    store.add_candidate(make_video("p1"))            # still pending
    video = store.get("p1")
    with pytest.raises(DownloadError, match="pending"):
        download_video(cfg, store, video, downloader=lambda url, opts: {})


def test_download_approved_only_touches_approved_rows(cfg, store):
    store.add_candidate(make_video("p1"))
    store.add_candidate(make_video("p2"))
    store.approve("p2")
    store.add_candidate(make_video("p3"))
    store.reject("p3")

    seen = []

    def downloader(url, opts):
        seen.append(url)
        outdir = Path(opts["outtmpl"]).parent
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "p2.mp4").write_bytes(b"x")
        return {"duration": 30}

    download_approved(cfg, store, downloader=downloader)
    assert len(seen) == 1 and "p2" in seen[0]
    assert store.get("p2").status == DOWNLOADED


def test_publish_refuses_anything_not_downloaded(cfg, store):
    store.add_candidate(make_video("p1"))
    store.approve("p1")
    with pytest.raises(pipeline.PipelineError, match="expected downloaded"):
        pipeline.publish_video(cfg, store, store.get("p1"), publisher=FakePublisher())


def test_publish_marks_missing_file_as_failed(cfg, store):
    store.add_candidate(make_video("p1"))
    store.approve("p1")
    store.set_status("p1", DOWNLOADED, video_path="/nowhere/nope.mp4")
    with pytest.raises(pipeline.PipelineError, match="missing"):
        pipeline.publish_video(cfg, store, store.get("p1"), publisher=FakePublisher())
    assert store.get("p1").status == FAILED


# ------------------------------------------------------------------ posting


def test_publish_records_the_post(cfg, store, downloaded):
    publisher = FakePublisher()
    result = pipeline.publish_video(cfg, store, downloaded, publisher=publisher)
    video = store.get("v1")
    assert result.media_id == "123"
    assert video.status == POSTED
    assert video.ig_permalink == "https://instagram.com/reel/abc/"
    assert video.posted_at and video.caption
    assert publisher.calls[0][1] == video.caption


def test_publish_failure_is_recorded_and_retryable(cfg, store, downloaded):
    with pytest.raises(pipeline.PipelineError):
        pipeline.publish_video(cfg, store, downloaded, publisher=FakePublisher(fail=True))
    video = store.get("v1")
    assert video.status == FAILED and video.attempts == 1 and "nope" in video.error

    assert pipeline.retry_failed(cfg, store) == 1
    assert store.get("v1").status == DOWNLOADED


def test_retry_gives_up_after_max_attempts(cfg, store, downloaded):
    cfg.publish.max_attempts = 2
    store.set_status("v1", FAILED, attempts=2, error="boom")
    assert pipeline.retry_failed(cfg, store) == 0
    assert store.get("v1").status == FAILED


def test_delete_after_post_removes_the_file(cfg, store, downloaded):
    cfg.publish.delete_after_post = True
    path = Path(downloaded.video_path)
    pipeline.publish_video(cfg, store, downloaded, publisher=FakePublisher())
    assert not path.exists()


# ------------------------------------------------------------- rate limiting


def test_daily_limit(cfg, store):
    cfg.publish.max_per_day = 1
    cfg.publish.min_minutes_between_posts = 0
    assert pipeline.can_post_now(cfg, store)[0] is True

    store.add_candidate(make_video("d1"))
    store.set_status("d1", POSTED, posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    allowed, reason = pipeline.can_post_now(cfg, store)
    assert allowed is False and "daily limit" in reason


def test_minimum_gap_between_posts(cfg, store):
    cfg.publish.max_per_day = 10
    cfg.publish.min_minutes_between_posts = 120
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    store.add_candidate(make_video("d1"))
    store.set_status("d1", POSTED, posted_at=recent.isoformat(timespec="seconds"))

    allowed, reason = pipeline.can_post_now(cfg, store)
    assert allowed is False and "too soon" in reason

    older = datetime.now(timezone.utc) - timedelta(minutes=200)
    store.update("d1", posted_at=older.isoformat(timespec="seconds"))
    assert pipeline.can_post_now(cfg, store)[0] is True


def test_posting_hours(cfg, store):
    now = datetime.now(timezone.utc)
    cfg.publish.posting_hours = [(now.astimezone().hour + 5) % 24]
    allowed, reason = pipeline.can_post_now(cfg, store, now=now)
    assert allowed is False and "posting hours" in reason

    cfg.publish.posting_hours = [now.astimezone().hour]
    assert pipeline.can_post_now(cfg, store, now=now)[0] is True


def test_publish_due_respects_the_limit(cfg, store, downloaded, monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    cfg.publish.min_minutes_between_posts = 0
    cfg.publish.max_per_day = 10

    for vid in ("v2", "v3"):
        store.add_candidate(make_video(vid))
        store.approve(vid)
        path = cfg.resolve(cfg.download.dir) / f"{vid}.mp4"
        path.write_bytes(b"x")
        store.set_status(vid, DOWNLOADED, video_path=str(path))

    results = pipeline.publish_due(cfg, store, limit=2, publisher=FakePublisher())
    assert len(results) == 2
    assert store.counts()[POSTED] == 2 and store.counts()[DOWNLOADED] == 1


def test_publish_due_stops_when_the_budget_is_spent(cfg, store, downloaded):
    cfg.publish.max_per_day = 0
    cfg.publish.min_minutes_between_posts = 0
    store.add_candidate(make_video("x1"))
    store.set_status("x1", POSTED, posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert pipeline.publish_due(cfg, store, publisher=FakePublisher()) == []
    assert store.get("v1").status == DOWNLOADED


def test_next_publishable_picks_the_oldest(cfg, store, downloaded):
    store.add_candidate(make_video("v9"))
    store.approve("v9")
    store.set_status("v9", DOWNLOADED, video_path="/tmp/x.mp4")
    assert pipeline.next_publishable(store).id == "v1"


# ----------------------------------------------------------------- full pass


def test_run_once_never_posts_unreviewed_videos(cfg, store):
    """The whole point: a discovered video sits in `pending` until a human acts."""
    entries = {"entries": [{"id": "n1", "title": "new", "duration": 20, "view_count": 10,
                            "url": "n1"}]}
    import reelbot.discover as discover_mod

    def extractor(url, opts):
        return entries

    original = discover_mod.extract
    discover_mod.extract = extractor
    try:
        summary = pipeline.run_once(cfg, store, publisher=FakePublisher())
    finally:
        discover_mod.extract = original

    assert summary["discovered"] == 1
    assert summary["downloaded"] == 0 and summary["published"] == 0
    assert store.get("n1").status == PENDING


def test_max_per_day_none_means_unlimited(cfg, store):
    cfg.publish.max_per_day = None
    cfg.publish.min_minutes_between_posts = 0
    for vid in ("u1", "u2", "u3"):
        store.add_candidate(make_video(vid))
        store.set_status(
            vid, POSTED, posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    assert pipeline.can_post_now(cfg, store)[0] is True


def test_delete_after_post_removes_every_artifact(cfg, store, downloaded):
    """Cover frames and thumbnails were being left behind to fill the disk."""
    cfg.publish.delete_after_post = True
    outdir = cfg.resolve(cfg.download.dir)
    original = Path(downloaded.video_path)
    cover = outdir / "v1.reel.cover.jpg"
    thumb = outdir / "v1.webp"
    normalized = outdir / "v1.reel.mp4"
    for extra in (cover, thumb, normalized):
        extra.write_bytes(b"x" * 100)
    store.update("v1", thumb_path=str(thumb))

    # A different video's files must survive.
    bystander = outdir / "v2.mp4"
    bystander.write_bytes(b"keep me")

    pipeline.publish_video(cfg, store, store.get("v1"), publisher=FakePublisher())

    for gone in (original, cover, thumb, normalized):
        assert not gone.exists(), f"{gone.name} should have been deleted"
    assert bystander.exists()

    posted = store.get("v1")
    assert posted.status == POSTED
    assert posted.video_path is None and posted.thumb_path is None


def test_files_are_kept_by_default(cfg, store, downloaded):
    assert cfg.publish.delete_after_post is False
    pipeline.publish_video(cfg, store, downloaded, publisher=FakePublisher())
    assert Path(downloaded.video_path).exists()
