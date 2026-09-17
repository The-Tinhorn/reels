import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelbot.config import Config, Source  # noqa: E402
from reelbot.store import Store, Video, utcnow  # noqa: E402


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(path=str(tmp_path / "config.yaml"))
    config.db_path = "db.sqlite"
    config.download.dir = "downloads"
    config.publish.backend = "dryrun"
    config.sources = [Source(name="test", url="https://example.invalid/shorts")]
    return config


@pytest.fixture
def store(cfg: Config) -> Store:
    with Store(cfg.resolve(cfg.db_path)) as s:
        yield s


def make_video(video_id: str = "abc123", **overrides) -> Video:
    fields = dict(
        id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="A test short",
        channel="Test Channel",
        duration=30,
        view_count=1000,
        source="test",
        discovered_at=utcnow(),
    )
    fields.update(overrides)
    return Video(**fields)
