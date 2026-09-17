import os
from pathlib import Path

import pytest

from reelbot.config import ConfigError, expand_env, load_config, load_dotenv


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_file_is_nearly_empty(tmp_path):
    cfg = load_config(write(tmp_path / "c.yaml", "sources: []\n"))
    assert cfg.publish.backend == "dryrun"
    assert cfg.filters.max_duration == 90
    assert cfg.media.target_height == 1920


def test_env_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("IG_USERNAME", "mel")
    cfg = load_config(
        write(tmp_path / "c.yaml", "instagram:\n  username: ${IG_USERNAME}\n")
    )
    assert cfg.instagram.username == "mel"


def test_env_expansion_default_value(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    assert expand_env("${NOT_SET_ANYWHERE:-fallback}") == "fallback"
    assert expand_env("${NOT_SET_ANYWHERE}") == ""
    assert expand_env({"a": ["${NOT_SET_ANYWHERE:-x}"]}) == {"a": ["x"]}


def test_dotenv_does_not_override_real_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEP_ME", "from-env")
    write(tmp_path / ".env", "KEEP_ME=from-file\nNEW_ONE=hello\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["KEEP_ME"] == "from-env"
    assert os.environ["NEW_ONE"] == "hello"


def test_unknown_option_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(write(tmp_path / "c.yaml", "publish:\n  backend: dryrun\n  nope: 1\n"))


def test_bad_backend_rejected(tmp_path):
    with pytest.raises(ConfigError, match="publish.backend"):
        load_config(write(tmp_path / "c.yaml", "publish:\n  backend: tiktok\n"))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_source_needs_url(tmp_path):
    with pytest.raises(ConfigError, match="no url"):
        load_config(write(tmp_path / "c.yaml", "sources:\n  - name: x\n    url: ''\n"))


def test_relative_paths_resolve_against_the_config(tmp_path):
    cfg = load_config(write(tmp_path / "c.yaml", "db_path: data/x.db\n"))
    assert cfg.resolve(cfg.db_path) == tmp_path.resolve() / "data" / "x.db"
    assert cfg.resolve("/tmp/abs.db") == Path("/tmp/abs.db")


def test_shipped_template_parses(tmp_path):
    template = Path(__file__).resolve().parent.parent / "reelbot" / "templates" / "config.example.yaml"
    cfg = load_config(template)
    assert cfg.publish.backend == "dryrun"     # safe by default
    assert cfg.sources and cfg.sources[0].auto_approve is False
