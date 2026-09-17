"""Publishing backends."""

from __future__ import annotations

from reelbot.config import Config
from reelbot.publishers.base import Publisher, PublishError, PublishResult


def get_publisher(cfg: Config) -> Publisher:
    """Instantiate the backend named by ``publish.backend``."""
    backend = cfg.publish.backend
    if backend == "instagrapi":
        from reelbot.publishers.instagrapi_backend import InstagrapiPublisher

        return InstagrapiPublisher(cfg)
    if backend == "graph":
        from reelbot.publishers.graph_backend import GraphPublisher

        return GraphPublisher(cfg)
    if backend == "dryrun":
        from reelbot.publishers.dryrun import DryRunPublisher

        return DryRunPublisher(cfg)
    raise PublishError(f"unknown publish backend: {backend}")


__all__ = ["Publisher", "PublishError", "PublishResult", "get_publisher"]
