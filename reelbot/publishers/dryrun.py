"""A publisher that writes what it *would* post. Default backend, and useful for tests."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from reelbot.config import Config
from reelbot.publishers.base import PublishResult

log = logging.getLogger(__name__)


class DryRunPublisher:
    name = "dryrun"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.outdir = cfg.resolve("data/dryrun")

    def check(self) -> None:
        log.info("dry-run backend: nothing will be posted to Instagram")

    def publish(
        self, video_path: Path, caption: str, thumbnail: Optional[Path] = None
    ) -> PublishResult:
        self.outdir.mkdir(parents=True, exist_ok=True)
        media_id = uuid.uuid4().hex[:16]
        record = {
            "media_id": media_id,
            "video": str(video_path),
            "size_bytes": video_path.stat().st_size if Path(video_path).exists() else 0,
            "thumbnail": str(thumbnail) if thumbnail else None,
            "caption": caption,
            "share_to_feed": self.cfg.publish.share_to_feed,
        }
        (self.outdir / f"{media_id}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        log.info("[dry-run] would post %s\n--- caption ---\n%s\n---------------",
                 video_path.name, caption)
        return PublishResult(
            media_id=media_id,
            permalink=f"dryrun://{media_id}",
            backend=self.name,
            raw=record,
        )
