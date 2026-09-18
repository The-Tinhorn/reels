"""Orchestration: discover → (human approval) → download → normalize → publish."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from reelbot import discover, download, media
from reelbot.caption import build_caption
from reelbot.config import Config
from reelbot.publishers import Publisher, PublishError, PublishResult, get_publisher
from reelbot.store import (
    APPROVED,
    DOWNLOADED,
    FAILED,
    POSTED,
    Store,
    Video,
    utcnow,
)

log = logging.getLogger(__name__)


class PipelineError(Exception):
    pass


# --------------------------------------------------------------------- limits


def can_post_now(cfg: Config, store: Store, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Check the posting budget. Returns (allowed, human-readable reason)."""
    now = now or datetime.now(timezone.utc)
    limits = cfg.publish

    if limits.max_per_day is not None:
        posted = store.posts_today(now)
        if posted >= limits.max_per_day:
            return False, f"daily limit reached ({posted}/{limits.max_per_day} in the last 24h)"

    if limits.min_minutes_between_posts:
        last = store.last_post_time()
        if last:
            earliest = last + timedelta(minutes=limits.min_minutes_between_posts)
            if now < earliest:
                wait = int((earliest - now).total_seconds() // 60)
                return False, f"too soon after the last post ({wait} min to go)"

    if limits.posting_hours:
        hour = now.astimezone().hour  # posting_hours are local time
        if hour not in limits.posting_hours:
            allowed = ", ".join(str(h) for h in sorted(limits.posting_hours))
            return False, f"outside posting hours (now {hour}:00 local, allowed: {allowed})"

    return True, "ok"


def next_publishable(store: Store) -> Optional[Video]:
    """Oldest downloaded-and-approved video waiting to go out."""
    queue = store.list(status=DOWNLOADED, order="reviewed_at ASC, discovered_at ASC", limit=1)
    return queue[0] if queue else None


# -------------------------------------------------------------------- publish


def _thumbnail_for(video: Video, video_path: Path, cfg: Config) -> Optional[Path]:
    """A JPEG cover frame, since Instagram will not take .webp thumbnails."""
    if video.thumb_path:
        existing = Path(video.thumb_path)
        if existing.exists() and existing.suffix.lower() in (".jpg", ".jpeg"):
            return existing
    dest = video_path.with_name(f"{video_path.stem}.cover.jpg")
    if dest.exists():
        return dest
    return media.cover_frame(video_path, dest, cfg.media, at=min(1.0, max(0.0, video.duration / 4)))


def _cleanup(cfg: Config, store: Store, video: Video, upload_path: Path) -> None:
    """Delete every file this video produced, once it is safely posted.

    That is the download, the normalized copy, the cover frame and the
    thumbnail — everything is named ``<video id>.*`` in the download
    directory, so a glob catches the lot rather than just the two paths the
    database happens to know about.
    """
    outdir = cfg.resolve(cfg.download.dir)
    paths = {Path(video.video_path or ""), upload_path, Path(video.thumb_path or "")}
    paths.update(outdir.glob(f"{video.id}.*"))

    freed = 0
    for path in sorted(p for p in paths if str(p)):
        try:
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
                log.debug("deleted %s", path)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.warning("could not delete %s: %s", path, exc)

    # The files are gone; stop pointing the review UI at them.
    store.update(video.id, video_path=None, thumb_path=None)
    if freed:
        log.info("freed %.1f MB after posting %s", freed / 1_000_000, video.id)


def publish_video(
    cfg: Config,
    store: Store,
    video: Video,
    publisher: Optional[Publisher] = None,
) -> PublishResult:
    """Normalize and publish one downloaded video. Marks it posted or failed."""
    if video.status != DOWNLOADED:
        raise PipelineError(f"{video.id} is {video.status}, expected {DOWNLOADED}")
    if not video.video_path or not Path(video.video_path).exists():
        store.set_status(video.id, FAILED, error="publish: video file is missing")
        raise PipelineError(f"{video.id}: video file missing ({video.video_path})")

    publisher = publisher or get_publisher(cfg)
    source_path = Path(video.video_path)

    try:
        upload_path = media.prepare(source_path, cfg)
    except media.MediaError as exc:
        store.set_status(
            video.id, FAILED, error=f"normalize: {exc}", attempts=video.attempts + 1
        )
        raise PipelineError(f"normalize failed for {video.id}: {exc}") from exc

    caption = build_caption(video, cfg.caption)
    thumbnail = _thumbnail_for(video, upload_path, cfg)

    try:
        result = publisher.publish(upload_path, caption, thumbnail=thumbnail)
    except PublishError as exc:
        attempts = video.attempts + 1
        store.set_status(video.id, FAILED, error=f"publish: {exc}", attempts=attempts)
        log.error("publish failed for %s (attempt %d): %s", video.id, attempts, exc)
        raise PipelineError(str(exc)) from exc

    store.set_status(
        video.id,
        POSTED,
        posted_at=utcnow(),
        ig_media_id=result.media_id,
        ig_permalink=result.permalink,
        caption=caption,
        error=None,
    )
    store.log(video.id, "published", {"backend": result.backend, "permalink": result.permalink})
    log.info("posted %s -> %s", video.id, result.permalink or result.media_id)

    if cfg.publish.delete_after_post:
        _cleanup(cfg, store, video, upload_path)
    return result


def publish_due(
    cfg: Config,
    store: Store,
    limit: Optional[int] = None,
    publisher: Optional[Publisher] = None,
    force: bool = False,
) -> List[PublishResult]:
    """Publish as many queued videos as the rate limits currently allow."""
    results: List[PublishResult] = []
    publisher = publisher or get_publisher(cfg)

    while limit is None or len(results) < limit:
        if not force:
            allowed, reason = can_post_now(cfg, store)
            if not allowed:
                log.info("not posting: %s", reason)
                break

        video = next_publishable(store)
        if video is None:
            log.info("nothing approved and downloaded is waiting to be posted")
            break

        try:
            results.append(publish_video(cfg, store, video, publisher=publisher))
        except PipelineError:
            continue  # already recorded as failed; move on to the next one

        if limit is None or len(results) < limit:
            # A little jitter between uploads in the same batch.
            time.sleep(random.uniform(5, 15))

    return results


# ------------------------------------------------------------------ full runs


def retry_failed(cfg: Config, store: Store) -> int:
    """Re-queue failures that still have attempts left."""
    requeued = 0
    for video in store.list(status=FAILED):
        if video.attempts >= cfg.publish.max_attempts:
            continue
        target = DOWNLOADED if video.video_path and Path(video.video_path).exists() else APPROVED
        store.set_status(video.id, target, error=None)
        requeued += 1
    if requeued:
        log.info("re-queued %d failed video(s)", requeued)
    return requeued


def run_once(
    cfg: Config,
    store: Store,
    do_discover: bool = True,
    do_download: bool = True,
    do_publish: bool = True,
    publish_limit: Optional[int] = None,
    publisher: Optional[Publisher] = None,
) -> dict:
    """One full pass of the pipeline."""
    summary = {"discovered": 0, "skipped": 0, "downloaded": 0, "published": 0, "pending": 0}

    if do_discover and cfg.sources:
        added, skipped = discover.discover_all(cfg, store)
        summary["discovered"], summary["skipped"] = added, skipped

    if do_download:
        # Only download what a human (or an auto_approve source) has approved.
        summary["downloaded"] = len(download.download_approved(cfg, store))

    if do_publish:
        summary["published"] = len(
            publish_due(cfg, store, limit=publish_limit, publisher=publisher)
        )

    summary["pending"] = store.counts().get("pending", 0)
    if summary["pending"]:
        log.info("%d video(s) waiting for review — run `reelbot review`", summary["pending"])
    return summary


def run_loop(
    cfg: Config,
    store: Store,
    interval_minutes: int = 30,
    max_iterations: Optional[int] = None,
    **kwargs,
) -> None:
    """Run the pipeline forever, sleeping *interval_minutes* between passes."""
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        log.info("--- pipeline pass %d ---", iteration)
        try:
            summary = run_once(cfg, store, **kwargs)
            log.info("pass %d: %s", iteration, summary)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.exception("pipeline pass failed: %s", exc)
            store.log(None, "pipeline_error", str(exc))

        if max_iterations is not None and iteration >= max_iterations:
            break
        sleep_for = interval_minutes * 60 + random.uniform(0, 60)
        log.info("sleeping %.1f minutes", sleep_for / 60)
        time.sleep(sleep_for)
