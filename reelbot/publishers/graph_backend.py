"""Optional official backend: the Meta Graph API.

This one *does* need API keys (a Business/Creator IG account linked to a
Facebook Page, plus a long-lived access token), and Meta has to be able to
fetch the video over HTTPS, so the file must be reachable at
``{graph.public_base_url}/{filename}``. Included for people who want the
supported route; ``instagrapi`` is the default precisely because it needs none
of this.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from reelbot.config import Config
from reelbot.publishers.base import PublishError, PublishResult

log = logging.getLogger(__name__)

API_VERSION = "v21.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"


class GraphPublisher:
    name = "graph"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.graph = cfg.graph

    def _requests(self):
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise PublishError("the graph backend needs `pip install requests`") from exc
        return requests

    def check(self) -> None:
        if not self.graph.ig_user_id or not self.graph.access_token:
            raise PublishError("graph.ig_user_id and graph.access_token are required")
        if not self.graph.public_base_url:
            raise PublishError(
                "graph.public_base_url is required — Meta downloads the video from a public URL"
            )
        requests = self._requests()
        resp = requests.get(
            f"{BASE_URL}/{self.graph.ig_user_id}",
            params={"fields": "username", "access_token": self.graph.access_token},
            timeout=30,
        )
        if resp.status_code != 200:
            raise PublishError(f"Graph API check failed: {resp.status_code} {resp.text[:300]}")
        log.info("authenticated as @%s", resp.json().get("username", "?"))

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        requests = self._requests()
        resp = requests.post(f"{BASE_URL}/{path}", data=data, timeout=120)
        if resp.status_code >= 400:
            raise PublishError(f"Graph API {path} failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def publish(
        self, video_path: Path, caption: str, thumbnail: Optional[Path] = None
    ) -> PublishResult:
        self.check()
        video_url = f"{self.graph.public_base_url.rstrip('/')}/{quote(Path(video_path).name)}"

        container = self._post(
            f"{self.graph.ig_user_id}/media",
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": str(self.cfg.publish.share_to_feed).lower(),
                "access_token": self.graph.access_token,
            },
        )
        container_id = container.get("id")
        if not container_id:
            raise PublishError(f"no container id in response: {container}")

        self._await_ready(container_id)

        published = self._post(
            f"{self.graph.ig_user_id}/media_publish",
            {"creation_id": container_id, "access_token": self.graph.access_token},
        )
        media_id = published.get("id", "")
        return PublishResult(
            media_id=str(media_id),
            permalink=self._permalink(media_id),
            backend=self.name,
            raw={"container_id": container_id, "video_url": video_url},
        )

    def _await_ready(self, container_id: str) -> None:
        """Poll the container until Meta finishes transcoding."""
        requests = self._requests()
        for attempt in range(self.graph.poll_attempts):
            resp = requests.get(
                f"{BASE_URL}/{container_id}",
                params={"fields": "status_code,status", "access_token": self.graph.access_token},
                timeout=30,
            )
            status = resp.json().get("status_code", "")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"container {container_id} failed: {resp.json()}")
            log.debug("container %s: %s (attempt %d)", container_id, status, attempt + 1)
            time.sleep(self.graph.poll_seconds)
        raise PublishError(f"container {container_id} never finished processing")

    def _permalink(self, media_id: str) -> str:
        if not media_id:
            return ""
        try:
            requests = self._requests()
            resp = requests.get(
                f"{BASE_URL}/{media_id}",
                params={"fields": "permalink", "access_token": self.graph.access_token},
                timeout=30,
            )
            return resp.json().get("permalink", "")
        except Exception:  # permalink is a nicety, never a failure
            return ""
