"""Instagram publishing without Meta API keys.

Uses instagrapi, which speaks Instagram's private mobile API with your own
account credentials — no Meta developer app, no Business account, no tokens.

Two things matter for staying logged in (and un-flagged):

* The device fingerprint and session are persisted to ``instagram.session_file``
  so every run looks like the same phone instead of a brand new login.
* Calls are spaced out with a random delay, and the pipeline enforces its own
  per-day / minimum-gap limits on top.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from reelbot.config import Config
from reelbot.publishers.base import PublishError, PublishResult

log = logging.getLogger(__name__)


class InstagrapiPublisher:
    name = "instagrapi"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ig = cfg.instagram
        self.session_file = cfg.resolve(self.ig.session_file)
        self._client: Optional[Any] = None

    # ------------------------------------------------------------------ client

    def _new_client(self) -> Any:
        try:
            from instagrapi import Client
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise PublishError(
                "instagrapi is not installed — `pip install instagrapi`, or set "
                "publish.backend to dryrun/graph"
            ) from exc

        delay = self.ig.delay_range or [2, 8]
        client = Client()
        client.delay_range = [int(delay[0]), int(delay[-1])]
        if self.ig.proxy:
            client.set_proxy(self.ig.proxy)
        return client

    def _verification_code(self) -> str:
        """Turn a stored TOTP seed into a current 6-digit code, if configured."""
        seed = (self.ig.verification_code or "").strip()
        if not seed:
            return ""
        if seed.isdigit() and len(seed) == 6:
            return seed  # already a one-time code
        try:
            return self._new_client().totp_generate_code(seed)
        except Exception as exc:
            raise PublishError(f"could not generate a 2FA code from the stored seed: {exc}") from exc

    def login(self, force: bool = False) -> Any:
        """Return a logged-in client, reusing the saved session where possible."""
        if self._client is not None and not force:
            return self._client

        if not self.ig.username or not self.ig.password:
            raise PublishError(
                "instagram.username / instagram.password are not set "
                "(put IG_USERNAME and IG_PASSWORD in your .env)"
            )

        from instagrapi.exceptions import LoginRequired

        client = self._new_client()
        code = self._verification_code()

        if self.session_file.exists() and not force:
            try:
                client.load_settings(self.session_file)
                client.login(self.ig.username, self.ig.password, verification_code=code)
                client.get_timeline_feed()  # cheap call that proves the session works
                log.info("reusing saved Instagram session for %s", self.ig.username)
                self._client = client
                return client
            except LoginRequired:
                log.warning("saved session expired — logging in again on the same device")
                settings = client.get_settings()
                client = self._new_client()
                # Keep the device UUIDs: a new device on every login looks suspicious.
                client.set_settings({})
                client.set_uuids(settings.get("uuids", {}))
            except Exception as exc:
                log.warning("could not reuse saved session (%s) — logging in fresh", exc)
                client = self._new_client()

        try:
            if not client.login(self.ig.username, self.ig.password, verification_code=code):
                raise PublishError("Instagram login returned false")
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"Instagram login failed: {exc}") from exc

        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        client.dump_settings(self.session_file)
        try:
            self.session_file.chmod(0o600)
        except OSError:  # pragma: no cover - filesystem dependent
            pass
        log.info("logged in to Instagram as %s (session saved)", self.ig.username)
        self._client = client
        return client

    # ----------------------------------------------------------------- publish

    def check(self) -> None:
        client = self.login()
        try:
            user_id = client.user_id
            info = client.user_info(user_id)
            log.info("authenticated as @%s (%s followers)", info.username, info.follower_count)
        except Exception as exc:
            raise PublishError(f"session check failed: {exc}") from exc

    def publish(
        self, video_path: Path, caption: str, thumbnail: Optional[Path] = None
    ) -> PublishResult:
        client = self.login()
        extra: Dict[str, Any] = {}
        if self.cfg.publish.share_to_feed:
            # Without this the Reel only shows up in the Reels tab, not the grid.
            extra["share_to_feed"] = True

        log.info("uploading %s as a Reel (%d char caption)", video_path.name, len(caption))
        try:
            media = client.clip_upload(
                Path(video_path),
                caption,
                thumbnail=Path(thumbnail) if thumbnail else None,
                extra_data=extra,
            )
        except Exception as exc:
            raise PublishError(f"clip_upload failed: {exc}") from exc

        code = getattr(media, "code", "") or ""
        return PublishResult(
            media_id=str(getattr(media, "pk", "") or ""),
            permalink=f"https://www.instagram.com/reel/{code}/" if code else "",
            backend=self.name,
            raw={"code": code, "thumbnail_url": str(getattr(media, "thumbnail_url", "") or "")},
        )
