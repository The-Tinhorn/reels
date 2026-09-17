import pytest

from reelbot.cli import parse_args


def test_global_flags_work_before_and_after_the_subcommand():
    before = parse_args(["-c", "x.yaml", "-v", "run"])
    after = parse_args(["run", "-c", "x.yaml", "-v"])
    assert before.config == after.config == "x.yaml"
    assert before.verbose == after.verbose == 1
    assert before.command == after.command == "run"


def test_quiet_after_the_subcommand():
    args = parse_args(["publish", "--quiet", "--dry-run"])
    assert args.quiet is True and args.dry_run is True


def test_subcommand_is_required():
    with pytest.raises(SystemExit):
        parse_args([])


def test_approve_takes_several_ids():
    args = parse_args(["approve", "a1", "a2", "--by", "mel"])
    assert args.ids == ["a1", "a2"] and args.by == "mel"


def test_publish_defaults_to_one_post():
    args = parse_args(["publish"])
    assert args.limit == 1 and args.force is False and args.dry_run is False


def test_run_loop_defaults():
    args = parse_args(["run", "--loop"])
    assert args.loop is True and args.interval == 30


def test_subcommand_does_not_clobber_a_global_flag_given_first():
    """argparse copies subparser defaults over the parent's parsed values."""
    args = parse_args(["--config", "mine.yaml", "status"])
    assert args.config == "mine.yaml"


def test_defaults_are_present_when_no_global_flag_is_given():
    args = parse_args(["status"])
    assert args.config == "config.yaml" and args.verbose == 0 and args.quiet is False
