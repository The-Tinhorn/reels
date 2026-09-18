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
    assert cfg.filters.max_duration == 180
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


def test_dotenv_last_value_wins_within_the_file():
    """The shipped .env ships empty placeholders; appending a value must work."""
    from reelbot.config import parse_dotenv

    values = parse_dotenv("REELBOT_REVIEW_PASSWORD=\nOTHER=1\nREELBOT_REVIEW_PASSWORD=hunter2\n")
    assert values["REELBOT_REVIEW_PASSWORD"] == "hunter2"
    assert values["OTHER"] == "1"


def test_dotenv_parsing_details():
    from reelbot.config import parse_dotenv

    values = parse_dotenv(
        '# a comment\n'
        'export EXPORTED=yes\n'
        'QUOTED="with spaces"\n'
        "SINGLE='single'\n"
        'EMPTY=\n'
        'WITH_EQUALS=a=b\n'
        'not a pair\n'
    )
    assert values == {
        "EXPORTED": "yes",
        "QUOTED": "with spaces",
        "SINGLE": "single",
        "EMPTY": "",
        "WITH_EQUALS": "a=b",
    }


def test_review_password_comes_from_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("REELBOT_REVIEW_PASSWORD", "hunter2")
    cfg = load_config(
        write(tmp_path / "c.yaml", "review:\n  password: ${REELBOT_REVIEW_PASSWORD:-}\n")
    )
    assert cfg.review.password == "hunter2"


# ------------------------------------------------- values arriving as strings
# Docker and CasaOS pass every setting as a string, so the loader has to
# convert them to the types the rest of the code expects.


def test_numbers_from_strings(tmp_path):
    cfg = load_config(write(tmp_path / "c.yaml", "publish:\n  max_per_day: '7'\n"))
    assert cfg.publish.max_per_day == 7 and isinstance(cfg.publish.max_per_day, int)


def test_booleans_from_strings(tmp_path):
    cfg = load_config(
        write(tmp_path / "c.yaml", "publish:\n  share_to_feed: 'false'\n"
                                   "  delete_after_post: 'yes'\n")
    )
    assert cfg.publish.share_to_feed is False
    assert cfg.publish.delete_after_post is True


def test_optional_int_from_an_empty_string_is_none(tmp_path):
    """An unset ${REELBOT_MAX_PER_DAY} expands to "" and must mean unlimited."""
    cfg = load_config(write(tmp_path / "c.yaml", "publish:\n  max_per_day: ''\n"))
    assert cfg.publish.max_per_day is None


def test_lists_from_a_single_string(tmp_path):
    cfg = load_config(
        write(tmp_path / "c.yaml", "publish:\n  posting_hours: '9, 13,19'\n"
                                   "caption:\n  hashtags: '#reels #shorts'\n")
    )
    assert cfg.publish.posting_hours == [9, 13, 19]
    assert cfg.caption.hashtags == ["#reels", "#shorts"]


def test_empty_list_string(tmp_path):
    cfg = load_config(write(tmp_path / "c.yaml", "publish:\n  posting_hours: ''\n"))
    assert cfg.publish.posting_hours == []


def test_a_bad_number_names_the_setting(tmp_path):
    with pytest.raises(ConfigError, match="publish.max_per_day"):
        load_config(write(tmp_path / "c.yaml", "publish:\n  max_per_day: 'three'\n"))


def test_a_bad_boolean_names_the_setting(tmp_path):
    with pytest.raises(ConfigError, match="publish.share_to_feed"):
        load_config(write(tmp_path / "c.yaml", "publish:\n  share_to_feed: 'maybe'\n"))


def test_yaml_native_types_still_work(tmp_path):
    cfg = load_config(
        write(tmp_path / "c.yaml", "publish:\n  max_per_day: 5\n  posting_hours: [9, 18]\n"
                                   "  share_to_feed: true\n")
    )
    assert cfg.publish.max_per_day == 5
    assert cfg.publish.posting_hours == [9, 18]
    assert cfg.publish.share_to_feed is True


def test_env_configures_the_template_end_to_end(monkeypatch):
    """The shipped template must be fully drivable from environment variables."""
    monkeypatch.setenv("REELBOT_SOURCE_URL", "https://www.youtube.com/@mel/shorts")
    monkeypatch.setenv("REELBOT_MAX_PER_DAY", "1")
    monkeypatch.setenv("REELBOT_POSTING_HOURS", "9,18")
    monkeypatch.setenv("REELBOT_HASHTAGS", "#reels #fyp")
    monkeypatch.setenv("REELBOT_BACKEND", "instagrapi")
    monkeypatch.setenv("REELBOT_PRESET", "veryfast")

    template = Path(__file__).resolve().parent.parent / "reelbot" / "templates" / "config.example.yaml"
    cfg = load_config(template)

    assert cfg.sources[0].url == "https://www.youtube.com/@mel/shorts"
    assert cfg.sources[0].limit == 20            # untouched default, still an int
    assert cfg.publish.max_per_day == 1
    assert cfg.publish.posting_hours == [9, 18]
    assert cfg.caption.hashtags == ["#reels", "#fyp"]
    assert cfg.publish.backend == "instagrapi"
    assert cfg.media.preset == "veryfast"
