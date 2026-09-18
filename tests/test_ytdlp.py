from pathlib import Path

from reelbot.ytdlp import (
    COOKIE_FILENAMES,
    apply_auth,
    find_cookies,
    looks_like_bot_check,
)

REAL_ERROR = (
    "ERROR: [youtube] 30xWHYr3LVk: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)


def test_recognises_the_bot_check(tmp_path):
    assert looks_like_bot_check(REAL_ERROR) is True
    assert looks_like_bot_check("Sign in to confirm you’re not a bot") is True  # curly quote
    assert looks_like_bot_check("HTTP Error 403: Forbidden") is False


def test_cookies_txt_beside_the_config_is_found_without_configuration(cfg, tmp_path):
    assert find_cookies(cfg) is None
    (cfg.root / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    assert find_cookies(cfg) == cfg.root / "cookies.txt"


def test_every_documented_filename_is_detected(cfg):
    for name in COOKIE_FILENAMES:
        path = cfg.root / name
        path.write_text("# Netscape HTTP Cookie File\n")
        assert find_cookies(cfg) == path
        path.unlink()


def test_an_explicit_path_wins(cfg):
    (cfg.root / "cookies.txt").write_text("auto")
    elsewhere = cfg.root / "mine.txt"
    elsewhere.write_text("explicit")
    cfg.download.cookies_file = "mine.txt"
    assert find_cookies(cfg) == elsewhere


def test_a_configured_file_that_is_missing_warns_and_falls_through(cfg, caplog):
    cfg.download.cookies_file = "nowhere.txt"
    with caplog.at_level("WARNING"):
        assert find_cookies(cfg) is None
    assert "does not exist" in caplog.text


def test_apply_auth_sets_the_ytdlp_options(cfg):
    (cfg.root / "cookies.txt").write_text("x")
    cfg.download.player_client = ["tv", "web_safari"]
    cfg.download.user_agent = "Mozilla/5.0 (test)"

    opts = apply_auth(cfg, {"quiet": True})
    assert opts["cookiefile"] == str(cfg.root / "cookies.txt")
    assert opts["extractor_args"]["youtube"]["player_client"] == ["tv", "web_safari"]
    assert opts["http_headers"]["User-Agent"] == "Mozilla/5.0 (test)"
    assert opts["quiet"] is True


def test_apply_auth_adds_nothing_when_unconfigured(cfg):
    assert apply_auth(cfg, {}) == {}


def test_both_discovery_and_download_pick_the_cookies_up(cfg):
    """The bot check hits either stage, so both must authenticate."""
    from reelbot.discover import _ydl_opts
    from reelbot.download import build_opts

    (cfg.root / "cookies.txt").write_text("x")
    expected = str(cfg.root / "cookies.txt")
    assert _ydl_opts(cfg, flat=True)["cookiefile"] == expected
    assert build_opts(cfg, Path(cfg.root))["cookiefile"] == expected


def test_player_client_from_a_comma_separated_string(tmp_path):
    """It arrives as one string from an environment variable."""
    from reelbot.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text("download:\n  player_client: 'tv, web_safari'\n")
    assert load_config(path).download.player_client == ["tv", "web_safari"]
