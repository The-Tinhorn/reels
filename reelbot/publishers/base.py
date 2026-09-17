"""Publisher interface shared by every backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Protocol


class PublishError(Exception):
    """Raised when a Reel could not be published."""


@dataclass
class PublishResult:
    media_id: str = ""
    permalink: str = ""
    backend: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    name: str

    def check(self) -> None:
        """Verify credentials/config up front. Raises PublishError when unusable."""

    def publish(
        self, video_path: Path, caption: str, thumbnail: Optional[Path] = None
    ) -> PublishResult:
        """Upload *video_path* as a Reel with *caption*."""
