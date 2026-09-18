"""Shared yt-dlp options — chiefly how we authenticate to YouTube.

YouTube increasingly answers automated requests with "Sign in to confirm you're
not a bot". The fix is cookies from a logged-in session. In a container nobody
can edit a config file comfortably, so a ``cookies.txt`` dropped beside
``config.yaml`` is picked up with no configuration at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from reelbot.config import Config

log = logging.getLogger(__name__)

#: Filenames treated as cookies when nothing is configured explicitly.
COOKIE_FILENAMES = ("cookies.txt", "youtube-cookies.txt", "youtube.com_cookies.txt")

BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you are not a bot",
)

COOKIE_HELP = (
    "YouTube is asking this machine to prove it is not a bot. Export cookies "
    "from a browser logged in to YouTube (a private window, using a throwaway "
    "account) in Netscape format, and save them as `cookies.txt` next to "
    "config.yaml — reelbot picks that up automatically. In Docker that is "
    "/DATA/AppData/reelbot/cookies.txt on the host."
)


def looks_like_bot_check(message: str) -> bool:
    lowered = str(message).lower()
    return any(marker in lowered for marker in BOT_CHECK_MARKERS)


def find_cookies(cfg: Config) -> Optional[Path]:
    """The cookie file to use: whatever is configured, else one found beside the config."""
    configured = cfg.download.cookies_file
    if configured:
        path = cfg.resolve(configured)
        if path.exists():
            return path
        log.warning("download.cookies_file is set to %s but that file does not exist", path)
        return None

    for name in COOKIE_FILENAMES:
        candidate = cfg.root / name
        if candidate.exists():
            log.info("using cookies from %s", candidate)
            return candidate
    return None


def apply_auth(cfg: Config, opts: Dict[str, Any]) -> Dict[str, Any]:
    """Add cookie and extractor options to a yt-dlp options dict, in place."""
    cookies = find_cookies(cfg)
    if cookies:
        opts["cookiefile"] = str(cookies)
    if cfg.download.cookies_from_browser:
        # Useless inside a container, but handy when running on a desktop.
        opts["cookiesfrombrowser"] = (cfg.download.cookies_from_browser,)

    clients = [c for c in (cfg.download.player_client or []) if c]
    if clients:
        opts.setdefault("extractor_args", {})
        opts["extractor_args"].setdefault("youtube", {})["player_client"] = clients

    if cfg.download.user_agent:
        opts.setdefault("http_headers", {})["User-Agent"] = cfg.download.user_agent
    return opts
