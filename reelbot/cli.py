"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

from reelbot import __version__, discover, download, media, pipeline, review
from reelbot.caption import build_caption
from reelbot.config import Config, ConfigError, load_config
from reelbot.publishers import PublishError, get_publisher
from reelbot.store import ALL_STATUSES, DOWNLOADED, PENDING, Store, Video

log = logging.getLogger("reelbot")

TEMPLATES = Path(__file__).parent / "templates"
CONFIG_TEMPLATE = TEMPLATES / "config.example.yaml"
ENV_TEMPLATE = TEMPLATES / "env.example"


def setup_logging(verbosity: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbosity else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("urllib3", "public_request", "private_request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _open(args) -> tuple[Config, Store]:
    cfg = load_config(args.config)
    return cfg, Store(cfg.resolve(cfg.db_path))


def _fmt_row(video: Video) -> str:
    title = (video.title or "")[:48]
    duration = f"{video.duration}s" if video.duration else "?"
    return f"{video.id:<12} {video.status:<11} {duration:>5} {video.channel[:18]:<18} {title}"


def _resolve_ids(store: Store, ids: List[str], status: Optional[str]) -> List[Video]:
    """Expand 'all' into every video in *status*, otherwise look ids up."""
    if len(ids) == 1 and ids[0] == "all":
        return store.list(status=status)
    videos = []
    for vid in ids:
        video = store.get(vid)
        if video is None:
            print(f"unknown id: {vid}", file=sys.stderr)
            continue
        videos.append(video)
    return videos


# --------------------------------------------------------------- subcommands


def cmd_init(args) -> int:
    dest = Path(args.config)
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite)")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CONFIG_TEMPLATE, dest)

    env_dest = dest.parent / ".env"
    if ENV_TEMPLATE.exists() and not env_dest.exists():
        shutil.copy(ENV_TEMPLATE, env_dest)
        try:
            env_dest.chmod(0o600)
        except OSError:
            pass
        print(f"wrote {env_dest} (fill in your Instagram login)")

    print(textwrap.dedent(f"""
        wrote {dest}

        Next:
          1. edit {dest} — set your source channel and caption template
          2. reelbot discover          find candidate Shorts
          3. reelbot review            approve the ones you want in a browser
          4. reelbot run               download the approved ones and post them

        publish.backend starts as `dryrun`, so nothing is posted until you
        change it to `instagrapi` and run `reelbot login`.
    """).strip())
    return 0


def cmd_discover(args) -> int:
    cfg, store = _open(args)
    with store:
        if args.url:
            video = discover.add_url(cfg, store, args.url, approve=args.approve)
            if video is None:
                print("could not read that URL", file=sys.stderr)
                return 1
            print(f"queued {video.id} ({video.status}) — {video.title}")
            return 0
        if not cfg.sources:
            print("no sources configured — add one to your config, or pass a URL",
                  file=sys.stderr)
            return 1
        if args.refilter:
            print(f"forgot {store.clear_filtered()} previously filtered video(s)")
        added, skipped = discover.discover_all(cfg, store)
        pending = store.counts().get(PENDING, 0)
        print(f"queued {added} new, skipped {skipped}; {pending} awaiting review")
    return 0


def cmd_list(args) -> int:
    cfg, store = _open(args)
    with store:
        videos = store.list(status=None if args.status == "all" else args.status,
                            limit=args.limit, order="discovered_at DESC")
        if not videos:
            print(f"nothing with status {args.status!r}")
            return 0
        print(f"{'ID':<12} {'STATUS':<11} {'LEN':>5} {'CHANNEL':<18} TITLE")
        for video in videos:
            print(_fmt_row(video))
        print(f"\n{len(videos)} video(s)")
    return 0


def cmd_show(args) -> int:
    cfg, store = _open(args)
    with store:
        video = store.get(args.id)
        if video is None:
            print(f"unknown id: {args.id}", file=sys.stderr)
            return 1
        for key, value in video.to_dict().items():
            if value not in (None, "", 0):
                print(f"{key:<15} {value}")
        print(f"\n--- caption preview ---\n{build_caption(video, cfg.caption)}")
        if video.video_path and Path(video.video_path).exists():
            try:
                print(f"\n--- media ---\n{media.media_summary(Path(video.video_path), cfg.media)}")
            except media.MediaError as exc:
                print(f"\n(probe failed: {exc})")
    return 0


def cmd_approve(args) -> int:
    cfg, store = _open(args)
    with store:
        count = 0
        for video in _resolve_ids(store, args.ids, PENDING):
            if store.approve(video.id, by=args.by, caption=args.caption):
                count += 1
                print(f"approved {video.id} — {video.title[:60]}")
            else:
                print(f"cannot approve {video.id} (status: {video.status})", file=sys.stderr)
        print(f"{count} approved")
    return 0


def cmd_reject(args) -> int:
    cfg, store = _open(args)
    with store:
        count = 0
        for video in _resolve_ids(store, args.ids, PENDING):
            if store.reject(video.id, by=args.by):
                count += 1
                print(f"rejected {video.id}")
            else:
                print(f"cannot reject {video.id} (status: {video.status})", file=sys.stderr)
        print(f"{count} rejected")
    return 0


def cmd_review(args) -> int:
    cfg, store = _open(args)
    with store:
        try:
            review.serve(
                cfg, store,
                host=args.host, port=args.port,
                open_browser=not args.no_browser,
                password=args.password,
                allow_insecure=args.insecure,
            )
        except review.ReviewError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


def cmd_download(args) -> int:
    cfg, store = _open(args)
    with store:
        done = download.download_approved(cfg, store, limit=args.limit)
        print(f"downloaded {len(done)} video(s)")
        for video in done:
            print(f"  {video.id} -> {video.video_path}")
    return 0


def cmd_publish(args) -> int:
    cfg, store = _open(args)
    with store:
        if args.dry_run:
            cfg.publish.backend = "dryrun"
        if not args.force:
            allowed, reason = pipeline.can_post_now(cfg, store)
            if not allowed:
                print(f"not posting: {reason} (use --force to override)")
                return 0
        try:
            publisher = get_publisher(cfg)
        except PublishError as exc:
            print(f"publisher unavailable: {exc}", file=sys.stderr)
            return 1

        if args.id:
            video = store.get(args.id)
            if video is None:
                print(f"unknown id: {args.id}", file=sys.stderr)
                return 1
            if video.status != DOWNLOADED:
                print(f"{video.id} is {video.status}, expected {DOWNLOADED}", file=sys.stderr)
                return 1
            try:
                result = pipeline.publish_video(cfg, store, video, publisher=publisher)
            except pipeline.PipelineError as exc:
                print(f"publish failed: {exc}", file=sys.stderr)
                return 1
            print(f"posted {video.id} -> {result.permalink or result.media_id}")
            return 0

        results = pipeline.publish_due(cfg, store, limit=args.limit,
                                       publisher=publisher, force=args.force)
        print(f"posted {len(results)} reel(s)")
        for result in results:
            print(f"  {result.permalink or result.media_id}")
    return 0


def cmd_run(args) -> int:
    cfg, store = _open(args)
    with store:
        if args.dry_run:
            cfg.publish.backend = "dryrun"

        httpd = None
        if args.with_review:
            # One process serving the approval UI while the pipeline loops —
            # this is what lets a single container do the whole job.
            try:
                httpd = review.serve_in_background(
                    cfg, store, host=args.review_host, password=args.review_password
                )
            except review.ReviewError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Review UI at {httpd.reelbot_url}")

        kwargs = dict(
            do_discover=not args.no_discover,
            do_download=not args.no_download,
            do_publish=not args.no_publish,
            publish_limit=args.limit,
        )
        try:
            if args.loop:
                pipeline.run_loop(cfg, store, interval_minutes=args.interval, **kwargs)
            else:
                summary = pipeline.run_once(cfg, store, **kwargs)
                print(
                    f"discovered {summary['discovered']}, downloaded {summary['downloaded']}, "
                    f"posted {summary['published']}, {summary['pending']} awaiting review"
                )
        finally:
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
    return 0


def cmd_retry(args) -> int:
    cfg, store = _open(args)
    with store:
        print(f"re-queued {pipeline.retry_failed(cfg, store)} failed video(s)")
    return 0


def cmd_status(args) -> int:
    cfg, store = _open(args)
    with store:
        counts = store.counts()
        allowed, reason = pipeline.can_post_now(cfg, store)
        print(f"config     {cfg.path}")
        print(f"database   {cfg.resolve(cfg.db_path)}")
        print(f"backend    {cfg.publish.backend}")
        print(f"ffmpeg     {'found' if media.have_ffmpeg(cfg.media) else 'NOT FOUND'}")
        print(f"sources    {len(cfg.sources)}")
        print()
        for status in ALL_STATUSES:
            print(f"  {status:<11} {counts.get(status, 0)}")
        print()
        budget = "unlimited" if cfg.publish.max_per_day is None else cfg.publish.max_per_day
        print(f"posted in the last 24h: {store.posts_today()}/{budget}")
        last = store.last_post_time()
        print(f"last post: {last.isoformat() if last else 'never'}")
        print(f"can post now: {'yes' if allowed else f'no — {reason}'}")

        if args.events:
            print("\nrecent events:")
            for event in store.recent_events(args.events):
                detail = f" — {event['detail']}" if event["detail"] else ""
                print(f"  {event['ts']}  {event['video_id'] or '-':<12} {event['kind']}{detail}")
    return 0


def cmd_login(args) -> int:
    cfg, store = _open(args)
    with store:
        if cfg.publish.backend == "dryrun":
            print("publish.backend is `dryrun` — set it to `instagrapi` first")
            return 1
        try:
            publisher = get_publisher(cfg)
            if args.force and hasattr(publisher, "login"):
                publisher.login(force=True)
            publisher.check()
        except PublishError as exc:
            print(f"login failed: {exc}", file=sys.stderr)
            return 1
        print("login ok — session saved")
    return 0


def cmd_caption(args) -> int:
    cfg, store = _open(args)
    with store:
        video = store.get(args.id)
        if video is None:
            print(f"unknown id: {args.id}", file=sys.stderr)
            return 1
        if args.set is not None:
            store.update(video.id, caption=args.set or None)
            print("caption updated")
            video = store.get(args.id)
        print(build_caption(video, cfg.caption))
    return 0


# ------------------------------------------------------------------- parsing


def build_parser() -> argparse.ArgumentParser:
    # Global flags live on a parent parser too, so both `reelbot -v run` and
    # `reelbot run -v` work. Their defaults are SUPPRESSed and filled in by
    # parse_args() afterwards: a subparser copies its own defaults over
    # whatever the top-level parser already parsed, which would turn
    # `reelbot -c mine.yaml run` back into the default config path.
    # set_defaults() cannot fix that either — `parents=` shares the very same
    # action objects, so it would clear the SUPPRESS on all of them at once.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=argparse.SUPPRESS,
                        help="path to config.yaml (default: config.yaml)")
    common.add_argument("--verbose", "-v", action="count", default=argparse.SUPPRESS,
                        help="debug logging")
    common.add_argument("--quiet", "-q", action="store_true", default=argparse.SUPPRESS,
                        help="warnings and errors only")

    parser = argparse.ArgumentParser(
        prog="reelbot",
        parents=[common],
        description="Download approved YouTube Shorts with yt-dlp and post them as Instagram Reels.",
    )
    parser.add_argument("--version", action="version", version=f"reelbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="write a starter config.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("discover", parents=[common], help="find candidate Shorts and queue them for review")
    p.add_argument("url", nargs="?", help="queue a single video URL instead of the sources")
    p.add_argument("--approve", action="store_true", help="approve that URL immediately")
    p.add_argument("--refilter", action="store_true",
                   help="re-evaluate videos that earlier filters rejected")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("list", parents=[common], help="list queued videos")
    p.add_argument("--status", default=PENDING, choices=[*ALL_STATUSES, "all"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", parents=[common], help="show everything known about one video")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", parents=[common], help="approve videos for posting")
    p.add_argument("ids", nargs="+", metavar="ID", help="video ids, or 'all'")
    p.add_argument("--caption", help="override the generated caption")
    p.add_argument("--by", default="cli")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", parents=[common], help="reject videos so they are never posted")
    p.add_argument("ids", nargs="+", metavar="ID", help="video ids, or 'all'")
    p.add_argument("--by", default="cli")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("review", parents=[common], help="open the local approval UI in a browser")
    p.add_argument("--host", help="bind address (default: review.host, 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default: review.port, 8765)")
    p.add_argument("--password", help="require this password (default: review.password)")
    p.add_argument("--insecure", action="store_true",
                   help="allow a non-local bind with no password")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("download", parents=[common], help="download approved videos")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("publish", parents=[common], help="post downloaded videos to Instagram")
    p.add_argument("--id", help="publish one specific video")
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--force", action="store_true", help="ignore the rate limits")
    p.add_argument("--dry-run", action="store_true", help="use the dryrun backend")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("run", parents=[common], help="one full pass: discover, download, publish")
    p.add_argument("--loop", action="store_true", help="keep running")
    p.add_argument("--interval", type=int, default=30, help="minutes between passes")
    p.add_argument("--limit", type=int, default=1, help="max posts per pass")
    p.add_argument("--no-discover", action="store_true")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--with-review", action="store_true",
                   help="also serve the approval UI from this process")
    p.add_argument("--review-host", help="bind address for --with-review")
    p.add_argument("--review-password", help="password for --with-review")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("retry", parents=[common], help="re-queue failed videos")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("status", parents=[common], help="queue counts and posting budget")
    p.add_argument("--events", type=int, default=0, help="also show N recent events")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("login", parents=[common], help="log in to Instagram and save the session")
    p.add_argument("--force", action="store_true", help="ignore the saved session")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("caption", parents=[common], help="preview or set a video's caption")
    p.add_argument("id")
    p.add_argument("--set", help="store a caption override ('' clears it)")
    p.set_defaults(func=cmd_caption)

    return parser


#: Global flags and the value to use when the flag appears nowhere.
GLOBAL_DEFAULTS = {"config": "config.yaml", "verbose": 0, "quiet": False}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except BrokenPipeError:
        # `reelbot list | head` closes the pipe early; exit quietly.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
