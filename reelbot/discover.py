"""Find candidate Shorts with yt-dlp and queue them for human review."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from reelbot.config import Config, Filters, Source
from reelbot.store import PENDING, APPROVED, Store, Video, utcnow

log = logging.getLogger(__name__)

#: Signature of a metadata extractor: (url, yt-dlp opts) -> info dict.
Extractor = Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]

#: Fields a filter may need that flat playlist extraction often omits.
ENRICHABLE = ("duration", "view_count", "upload_date")


def _ydl_opts(cfg: Config, flat: bool) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
        "noplaylist": False,
        "extract_flat": "in_playlist" if flat else False,
    }
    if cfg.download.cookies_file:
        opts["cookiefile"] = str(cfg.resolve(cfg.download.cookies_file))
    if cfg.download.cookies_from_browser:
        opts["cookiesfrombrowser"] = (cfg.download.cookies_from_browser,)
    return opts


def extract(url: str, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Thin wrapper around yt-dlp so tests can substitute a fake extractor."""
    from yt_dlp import YoutubeDL  # imported lazily: keeps unit tests dependency-free

    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _upload_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    raw = entry.get("upload_date")
    if raw:
        try:
            return datetime.strptime(str(raw), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def entry_to_video(entry: Dict[str, Any], source: Source) -> Video:
    upload_dt = _upload_datetime(entry)
    video_id = entry.get("id") or ""
    url = entry.get("webpage_url") or entry.get("url") or ""
    if url and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={video_id}"
    thumb = entry.get("thumbnail")
    if not thumb:
        thumbs = entry.get("thumbnails") or []
        thumb = thumbs[-1].get("url") if thumbs else ""
    return Video(
        id=video_id,
        url=url or f"https://www.youtube.com/watch?v={video_id}",
        title=entry.get("title") or "",
        description=(entry.get("description") or "")[:5000],
        channel=entry.get("channel") or entry.get("uploader") or "",
        channel_url=entry.get("channel_url") or entry.get("uploader_url") or "",
        duration=int(entry.get("duration") or 0),
        view_count=int(entry.get("view_count") or 0),
        like_count=int(entry.get("like_count") or 0),
        upload_date=upload_dt.date().isoformat() if upload_dt else "",
        thumbnail_url=thumb or "",
        source=source.name,
        status=PENDING,
        discovered_at=utcnow(),
    )


def check_filters(
    video: Video, filters: Filters, now: Optional[datetime] = None
) -> Optional[str]:
    """Return a rejection reason, or None when the video passes every filter."""
    now = now or datetime.now(timezone.utc)

    if video.duration and filters.max_duration and video.duration > filters.max_duration:
        return f"duration {video.duration}s > max {filters.max_duration}s"
    if video.duration and filters.min_duration and video.duration < filters.min_duration:
        return f"duration {video.duration}s < min {filters.min_duration}s"
    if filters.min_views and video.view_count < filters.min_views:
        return f"views {video.view_count} < min {filters.min_views}"

    if filters.published_within_days and video.upload_date:
        try:
            published = datetime.fromisoformat(video.upload_date).replace(tzinfo=timezone.utc)
        except ValueError:
            published = None
        if published and published < now - timedelta(days=filters.published_within_days):
            return f"published {video.upload_date}, older than {filters.published_within_days}d"

    title = (video.title or "").lower()
    if filters.title_allow and not any(term.lower() in title for term in filters.title_allow):
        return "title matches no title_allow term"
    for term in filters.title_deny:
        if term.lower() in title:
            return f"title matches denied term {term!r}"
    return None


def needs_enrichment(video: Video, filters: Filters) -> bool:
    """True when a filter depends on a field flat extraction did not provide."""
    if not video.duration and (filters.max_duration or filters.min_duration):
        return True
    if not video.view_count and filters.min_views:
        return True
    if not video.upload_date and filters.published_within_days:
        return True
    return False


def discover_source(
    cfg: Config,
    store: Store,
    source: Source,
    extractor: Optional[Extractor] = None,
    now: Optional[datetime] = None,
) -> Tuple[int, int]:
    """Queue new candidates from one source. Returns (added, skipped)."""
    extractor = extractor or extract
    opts = _ydl_opts(cfg, flat=True)
    if source.limit:
        opts["playlistend"] = source.limit

    log.info("discovering from %s (%s)", source.name, source.url)
    info = extractor(source.url, opts)
    if not info:
        log.warning("no data returned for source %s", source.name)
        return 0, 0

    entries: List[Dict[str, Any]] = info.get("entries") or ([info] if info.get("id") else [])
    added = skipped = 0

    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        video = entry_to_video(entry, source)
        # Already queued, or already rejected by these filters on an earlier run.
        if store.get(video.id) or store.is_filtered(video.id):
            skipped += 1
            continue

        if needs_enrichment(video, cfg.filters):
            full = extractor(video.url, _ydl_opts(cfg, flat=False))
            if full:
                video = entry_to_video(full, source)

        reason = check_filters(video, cfg.filters, now=now)
        if reason:
            log.debug("filtered out %s: %s", video.id, reason)
            store.mark_filtered(video.id, reason)
            skipped += 1
            continue

        if source.auto_approve:
            video.status = APPROVED
            video.reviewed_at = utcnow()
            video.reviewed_by = f"auto:{source.name}"

        if store.add_candidate(video):
            added += 1
            log.info("queued %s — %s (%s)", video.id, video.title[:60], video.status)
        else:
            skipped += 1

    return added, skipped


def discover_all(
    cfg: Config,
    store: Store,
    extractor: Optional[Extractor] = None,
) -> Tuple[int, int]:
    extractor = extractor or extract
    total_added = total_skipped = 0
    for source in cfg.sources:
        try:
            added, skipped = discover_source(cfg, store, source, extractor=extractor)
        except Exception as exc:  # one broken source must not stop the others
            log.error("source %s failed: %s", source.name, exc)
            store.log(None, "discover_error", f"{source.name}: {exc}")
            continue
        total_added += added
        total_skipped += skipped
    return total_added, total_skipped


def add_url(
    cfg: Config,
    store: Store,
    url: str,
    approve: bool = False,
    extractor: Optional[Extractor] = None,
) -> Optional[Video]:
    """Queue a single video URL by hand (bypasses source filters)."""
    extractor = extractor or extract
    info = extractor(url, _ydl_opts(cfg, flat=False))
    if not info:
        return None
    if info.get("entries"):
        info = info["entries"][0]
    video = entry_to_video(info, Source(name="manual", url=url))
    if approve:
        video.status = APPROVED
        video.reviewed_at = utcnow()
        video.reviewed_by = "manual"
    if not store.add_candidate(video):
        return store.get(video.id)
    return video
