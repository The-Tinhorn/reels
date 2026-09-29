"""Publish a local Reel through Meta's resumable Instagram upload API."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from reelbot.config import Config
from reelbot.publishers.base import PublishError, PublishResult

log = logging.getLogger(__name__)

API_VERSION = "v26.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"
MAX_REEL_BYTES = 1_000_000_000


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
        requests = self._requests()
        try:
            resp = requests.get(
                f"{BASE_URL}/{self.graph.ig_user_id}",
                params={"fields": "username", "access_token": self.graph.access_token},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PublishError(f"Graph API account check failed ({type(exc).__name__})") from exc
        log.info("authenticated as @%s", self._result(resp, "account check").get("username", "?"))

    def _safe(self, value: Any) -> str:
        message = str(value)
        if self.graph.access_token:
            message = message.replace(self.graph.access_token, "[redacted]")
        return message[:300]

    def _result(self, resp, action: str) -> Dict[str, Any]:
        if resp.status_code >= 400:
            raise PublishError(f"Meta {action} failed: {resp.status_code} {self._safe(resp.text)}")
        try:
            result = resp.json()
        except ValueError as exc:
            raise PublishError(f"Meta {action} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise PublishError(f"Meta {action} returned an unexpected response")
        if result.get("error"):
            raise PublishError(f"Meta {action} failed: {self._safe(result['error'])}")
        return result

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        requests = self._requests()
        try:
            resp = requests.post(f"{BASE_URL}/{path}", data=data, timeout=120)
        except requests.RequestException as exc:
            raise PublishError(f"Graph API {path} failed ({type(exc).__name__})") from exc
        return self._result(resp, path)

    def _upload(self, video_path: Path, uri: str, file_size: int) -> None:
        parsed = urlparse(uri)
        if parsed.scheme != "https" or parsed.netloc != "rupload.facebook.com":
            raise PublishError("Meta returned an invalid resumable upload URI")

        requests = self._requests()
        try:
            with video_path.open("rb") as stream:
                resp = requests.post(
                    uri,
                    data=stream,
                    headers={
                        "Authorization": f"OAuth {self.graph.access_token}",
                        "offset": "0",
                        "file_size": str(file_size),
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=(30, 600),
                    allow_redirects=False,
                )
        except (OSError, requests.RequestException) as exc:
            raise PublishError(f"Meta video upload failed ({type(exc).__name__})") from exc
        if resp.status_code >= 300:
            raise PublishError(f"Meta video upload failed: HTTP {resp.status_code}")
        result = self._result(resp, "video upload")
        if result.get("success") is not True:
            raise PublishError(f"Meta video upload was rejected: {result}")

    def publish(
        self, video_path: Path, caption: str, thumbnail: Optional[Path] = None
    ) -> PublishResult:
        video_path = Path(video_path)
        try:
            if not video_path.is_file():
                raise PublishError(f"video file is missing: {video_path}")
            file_size = video_path.stat().st_size
        except OSError as exc:
            raise PublishError(f"cannot read video file: {exc}") from exc
        if not 0 < file_size <= MAX_REEL_BYTES:
            raise PublishError("Meta Reels require a video file between 1 byte and 1 GB")

        self.check()
        container = self._post(
            f"{self.graph.ig_user_id}/media",
            {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": str(self.cfg.publish.share_to_feed).lower(),
                "access_token": self.graph.access_token,
            },
        )
        container_id = container.get("id")
        upload_uri = container.get("uri")
        if not container_id or not upload_uri:
            raise PublishError("Meta did not return a container ID and upload URI")

        self._upload(video_path, upload_uri, file_size)
        self._await_ready(container_id)

        published = self._post(
            f"{self.graph.ig_user_id}/media_publish",
            {"creation_id": container_id, "access_token": self.graph.access_token},
        )
        media_id = published.get("id")
        if not media_id:
            raise PublishError("Meta media_publish did not return a media ID")
        return PublishResult(
            media_id=str(media_id),
            permalink=self._permalink(media_id),
            backend=self.name,
            raw={"container_id": container_id},
        )

    def _await_ready(self, container_id: str) -> None:
        """Poll the container until Meta finishes transcoding."""
        requests = self._requests()
        for attempt in range(self.graph.poll_attempts):
            try:
                resp = requests.get(
                    f"{BASE_URL}/{container_id}",
                    params={"fields": "status_code,status", "access_token": self.graph.access_token},
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise PublishError(f"Meta container status check failed ({type(exc).__name__})") from exc
            result = self._result(resp, "container status check")
            status = result.get("status_code", "")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"container {container_id} failed: {self._safe(result)}")
            log.debug("container %s: %s (attempt %d)", container_id, status, attempt + 1)
            if attempt + 1 < self.graph.poll_attempts:
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
