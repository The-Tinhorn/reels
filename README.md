# reelbot

Downloads YouTube Shorts you've **approved** with `yt-dlp`, re-encodes them to
Reels spec with `ffmpeg`, and posts them to Instagram — without Meta API keys,
a Business account, or a developer app.

```
discover ──> pending ──[you approve]──> approved ──> download ──> normalize ──> post
                 │
                 └──[you reject]──> rejected      (never downloaded, never posted)
```

Nothing is downloaded or posted until a human approves it. Approval is a
recorded state transition (`reviewed_by`, `reviewed_at`), and both the
downloader and the publisher refuse anything that hasn't been through it.

## Before you start

Reposting someone else's video is their call, not yours — YouTube's terms, the
uploader's copyright, and Instagram's rules all apply, and "found it on
Shorts" is not a licence. This tool is built for reposting **your own** Shorts
to your own Reels, or content you have explicit permission to use. The
approval queue exists so that judgement happens per video, by you.

## Install

```bash
git clone https://github.com/The-Tinhorn/reels.git
cd reels
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# ffmpeg is required for normalization (and for yt-dlp to merge A/V streams)
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
```

Or install it as a command: `pip install -e .` gives you `reelbot` on your PATH.
Without that, every command below works as `python -m reelbot ...`.

## Quick start

```bash
reelbot init                  # writes config.yaml and .env
$EDITOR config.yaml           # point `sources` at your channel

reelbot discover              # queue candidate Shorts for review
reelbot review                # approve/reject in your browser (localhost:8765)
reelbot run --dry-run         # download + normalize the approved ones, post nothing
```

`publish.backend` starts as `dryrun`, which writes what it *would* post to
`data/dryrun/`. When the captions look right, switch to real posting:

```bash
$EDITOR .env                  # fill in IG_USERNAME / IG_PASSWORD
$EDITOR config.yaml           # under `publish:`, set backend: instagrapi
reelbot login                 # logs in once and saves the session
reelbot run                   # for real
```

## Commands

| Command | What it does |
| --- | --- |
| `reelbot init` | Write a starter `config.yaml` and `.env` |
| `reelbot discover` | Enumerate sources, filter, queue candidates as `pending` |
| `reelbot discover URL` | Queue one video by hand (`--approve` to skip review) |
| `reelbot discover --refilter` | Re-evaluate videos earlier filters rejected |
| `reelbot review` | Local web UI: watch, edit the caption, approve or reject |
| `reelbot list [--status]` | Show the queue |
| `reelbot show ID` | Everything known about one video, plus a caption preview |
| `reelbot approve ID... \| all` | Approve from the terminal (`--caption` to override) |
| `reelbot reject ID... \| all` | Reject — these are never downloaded |
| `reelbot download` | Download everything approved |
| `reelbot publish` | Post what's ready, if the rate limits allow (`--force` to ignore) |
| `reelbot run [--loop]` | Discover, download and publish in one pass |
| `reelbot status [--events N]` | Queue counts, posting budget, recent activity |
| `reelbot retry` | Re-queue failures that still have attempts left |
| `reelbot login` | Log in to Instagram and save the session |
| `reelbot caption ID [--set]` | Preview or override a caption |

Global flags (`-c/--config`, `-v/--verbose`, `-q/--quiet`) work before or after
the subcommand.

## The review UI

`reelbot review` serves a page on `127.0.0.1:8765` listing everything waiting.
Each card embeds the Short so you can watch it, shows duration/views/channel,
and gives you the generated caption in an editable box. **Approve** stores your
edited caption and moves it into the download queue; **Reject** takes it out
permanently. Downloaded and posted videos play back from local disk.

It binds to localhost and only ever serves files out of your download
directory. Don't expose it to the internet — it has no authentication.

## Configuration

`config.yaml`, with `${VAR}` filled in from the environment or a neighbouring
`.env`. Full annotated reference:
[`reelbot/templates/config.example.yaml`](reelbot/templates/config.example.yaml);
the credentials file is [`reelbot/templates/env.example`](reelbot/templates/env.example).

**sources** — anything `yt-dlp` can enumerate: a channel's `/shorts` tab, a
playlist, a search URL.

```yaml
sources:
  - name: my-channel
    url: https://www.youtube.com/@YOUR_HANDLE/shorts
    limit: 20
    auto_approve: false   # true skips human review — only for your own channel
```

**filters** — applied at discovery; failures never reach the queue.
`max_duration` (90s, the Reels cap), `min_duration`, `min_views`,
`published_within_days`, `title_allow`, `title_deny`. Rejects are
remembered, so an hourly run doesn't re-fetch them; after loosening a
filter, run `reelbot discover --refilter` to give them another look.

**caption** — a template over the video's metadata. Available tokens:
`{title}` `{channel}` `{channel_url}` `{url}` `{description}` `{views}`
`{upload_date}` `{hashtags}`. A caption edited in the review UI wins over the
template for that video.

**publish** — the throttle:

```yaml
publish:
  max_per_day: 3                  # null = unlimited, 0 pauses posting
  min_minutes_between_posts: 90
  posting_hours: [9, 13, 19]      # local time; empty = any hour
  share_to_feed: true             # also put the Reel on the profile grid
  delete_after_post: false
```

**review** — the approval UI's `host`, `port` and `password`. A password is
required before it may bind anywhere but localhost.

**media** — normalization targets. Shorts are usually already 1080×1920
H.264/AAC, and files that already conform are uploaded untouched. Anything else
is re-encoded to 9:16 with a blurred fill (`background: black` for letterbox
bars instead), trimmed to `max_duration`, and given a silent audio track if it
has none — Instagram rejects Reels without audio.

## Posting without Meta API keys

The default `instagrapi` backend talks to the same private mobile API the
Instagram app uses, authenticated with your own username and password. No
developer app, no Business account, no tokens, no public URL to host the file
at.

The trade-off is that this is not an API Meta supports, and automating a
personal account is against Instagram's terms. Accounts do get action-blocked.
What the tool does to keep that unlikely:

- **The session is saved** to `data/ig_session.json` and reused, along with the
  device fingerprint. Logging in fresh every run is the single fastest way to
  get flagged. `reelbot login` once; after that, runs reuse it silently and
  re-authenticate on the same device only when it expires.
- **Calls are spaced out** — `instagram.delay_range` jitters each API call, and
  `publish.max_per_day` / `min_minutes_between_posts` cap the pace above that.
- **Defaults are conservative**: 3 posts a day, 90 minutes apart.

Practical advice: start at one post a day for the first week, run from a stable
IP (set `instagram.proxy` to a residential proxy if the machine is in a
datacenter), and don't point it at a brand-new account.

**Two-factor auth:** put the TOTP *seed* (the "can't scan the code?" key) in
`IG_TOTP_SEED` and reelbot generates the current code at login. A plain 6-digit
code works too for a one-off `reelbot login`.

### The official alternative

If you'd rather stay on supported rails, `publish.backend: graph` uses the Meta
Graph API instead. That needs a Creator/Business account linked to a Facebook
Page, an app with `instagram_content_publish`, a long-lived token, and a public
HTTPS URL Meta can fetch the video from (`graph.public_base_url`). It's more
setup and more moving parts, which is why it isn't the default.

## Running it on a schedule

Either loop in the foreground:

```bash
reelbot run --loop --interval 60      # a pass every hour
```

…or let the system schedule it. Unit files are in [`deploy/`](deploy):

```bash
sudo cp deploy/reelbot.service deploy/reelbot.timer /etc/systemd/system/
sudo systemctl enable --now reelbot.timer
```

Or cron:

```cron
0 * * * * cd /home/you/reels && venv/bin/reelbot run --quiet >> data/reelbot.log 2>&1
```

Either way, approvals stay manual: the scheduled run only ever acts on what you
already approved, and new discoveries pile up in `pending` until you look at
them. Check in with `reelbot status`.

## CasaOS (one-click-ish)

CasaOS pulls images rather than building them, so
[a workflow](.github/workflows/docker.yml) publishes one to GitHub Container
Registry on every push. It is already built and publicly pullable — no login
needed:

```
ghcr.io/the-tinhorn/reels:latest        linux/amd64 + linux/arm64
```

1. In CasaOS: **App Store → Custom Install** (the ⊕ at the top right) →
   switch to the YAML/import view → paste
   [`docker-compose.casaos.yml`](docker-compose.casaos.yml).
2. Fill in the settings it shows you, at minimum:
   - `REELBOT_SOURCE_URL` — the channel to watch
   - `IG_USERNAME` / `IG_PASSWORD`
   - `REELBOT_REVIEW_PASSWORD` — the approval page refuses to start without one
   - `TZ` — so posting hours mean your local time
3. Install, then click the tile. It opens the approval queue.

(If a future build ever turns the package private, CasaOS will fail to pull it.
Flip it back at github.com/users/The-Tinhorn/packages → `reels` → Package
settings → Change visibility → Public.)

Leave `REELBOT_BACKEND` on `dryrun` at first — nothing is posted in that mode,
so you can watch what it picks and check the captions. Switch to `instagrapi`
when you're happy, and restart the app.

It's one container: the pipeline loops hourly and serves the approval page from
the same process. Everything it writes lives in `/DATA/AppData/reelbot`.

### Configuring it without touching files

Every setting worth changing is an environment variable, so it can all be done
from the CasaOS app settings:

| Variable | Default | What it does |
| --- | --- | --- |
| `REELBOT_SOURCE_URL` | — | Channel, playlist or search URL to watch |
| `REELBOT_BACKEND` | `dryrun` | `dryrun` posts nothing; `instagrapi` posts for real |
| `REELBOT_MAX_PER_DAY` | `3` | Posts per 24h. Empty = unlimited, `0` = paused |
| `REELBOT_MIN_MINUTES_BETWEEN_POSTS` | `90` | Minimum gap between posts |
| `REELBOT_POSTING_HOURS` | any | e.g. `9,13,19` — local time |
| `REELBOT_HASHTAGS` | `#reels #shorts` | Appended to every caption |
| `REELBOT_MAX_DURATION` | `90` | Skip anything longer, in seconds |
| `REELBOT_PUBLISHED_WITHIN_DAYS` | `30` | Ignore older Shorts. Empty = no limit |
| `REELBOT_PRESET` | `medium` | `veryfast` on a Pi |
| `REELBOT_REVIEW_PASSWORD` | — | Required for the approval page |
| `IG_USERNAME` / `IG_PASSWORD` | — | Your Instagram login |
| `IG_TOTP_SEED` | — | 2FA seed key, if the account has 2FA |
| `TZ` | `UTC` | Your timezone |

The container writes `config.yaml` into the volume on first start if it isn't
there. Edit that file directly for anything not in the table — it's the full
configuration, and environment variables simply fill in its placeholders.

To run a one-off command against a running app:

```bash
docker exec reelbot reelbot status
docker exec reelbot reelbot discover
docker exec reelbot reelbot list --status posted
```

## Docker (and Raspberry Pi)

A Pi is a good home for this — it's always on, and the workload is mostly
waiting. Use **64-bit Raspberry Pi OS**: on 32-bit armv7 several dependencies
have no prebuilt wheel and compile from source, which takes the better part of
an hour.

```bash
git clone -b claude/youtube-shorts-instagram-downloader-1lihx5 \
  https://github.com/The-Tinhorn/reels.git
cd reels
mkdir -p data                              # everything mutable lives here

docker compose build                       # ~5 min on a Pi 4
docker compose run --rm reelbot init       # writes data/config.yaml and data/.env
```

Edit `data/config.yaml` — point `sources` at your channel — and `data/.env`:

```
IG_USERNAME=your_handle
IG_PASSWORD=your_password
REELBOT_REVIEW_PASSWORD=pick-something     # the review UI needs this
```

Set your timezone so `posting_hours` means local time, then start both
containers:

```bash
echo "TZ=Europe/London" > .env             # host-level, for docker compose
docker compose up -d
```

Two services share one `./data` volume:

| Service | What it does |
| --- | --- |
| `reelbot` | A pipeline pass every hour: discover, download approved, publish what's due |
| `review` | The approval UI on port 8765 |

Open `http://<your-pi>:8765` from any machine on your network, log in with any
username and the password you set, and approve. Then:

```bash
docker compose logs -f reelbot             # watch it work
docker compose run --rm reelbot status     # queue counts and posting budget
docker compose run --rm reelbot discover   # a discovery pass right now
```

SQLite runs in WAL mode with a busy timeout, so both containers can use the
same database safely.

### Things worth knowing on a Pi

**The review UI has a password for a reason.** In a container it must bind to
`0.0.0.0` to be reachable at all, and it can approve posts. reelbot refuses to
start on a non-localhost address without one. Keep it on your LAN — don't
port-forward it or stick it on Tailscale Funnel.

**Encoding is the slow part.** Most Shorts are already 1080×1920 H.264/AAC and
are uploaded untouched, so the common case costs nothing. When a re-encode *is*
needed, a Pi 4 takes a few minutes per video at the default `preset: medium`.
Set `preset: veryfast` under `media:` — roughly 4× quicker for a few percent
more bitrate, which no one will see on a phone.

**File ownership.** The container starts as root only long enough to take
ownership of its data directory — CasaOS and `docker run -v` both create the
host directory as root — then drops to `PUID:PGID`, 1000:1000 by default. If
`id -u` says something else, set `PUID` and `PGID` to match.

**You'll see `data/data/downloads`.** Paths in the config resolve relative to
the config file, which lives in the volume root. Harmless; flatten it by
setting `db_path: reelbot.db` and `download.dir: downloads` if it bothers you.

**Updating:** `git pull && docker compose build && docker compose up -d`. Your
`data/` directory is untouched.

## How it's put together

```
reelbot/
  config.py       YAML + ${ENV} loading, typed dataclasses
  store.py        SQLite queue and state machine (thread-safe)
  discover.py     yt-dlp enumeration + filtering
  download.py     yt-dlp download (refuses unapproved videos)
  media.py        ffprobe/ffmpeg normalization to Reels spec
  caption.py      caption templating
  publishers/     instagrapi (no keys) | graph (official) | dryrun
  pipeline.py     orchestration, rate limits, retries
  review.py       the local approval UI
  cli.py          the command line
```

State lives in `data/reelbot.db`; videos in `data/downloads/`. Both are
git-ignored, as are `.env` and the saved session.

## Tests

```bash
pip install pytest && pytest
```

107 tests. They run offline — the yt-dlp network call is the only thing faked.
`tests/test_integration.py` drives the real pipeline end to end, including a
real ffmpeg re-encode and the real review server; the ffmpeg-dependent tests
skip themselves if it isn't installed.

## Troubleshooting

**`Sign in to confirm you're not a bot` from yt-dlp** — YouTube is challenging
the IP. Set `download.cookies_from_browser: firefox`, or export cookies to a
file and point `download.cookies_file` at it.

**A login challenge from Instagram** — open the app on your phone, approve the
login, then `reelbot login --force`.

**`ffmpeg not found`** — install it, or set `media.normalize: false` to upload
what yt-dlp produced (Instagram may reject it).

**A post failed** — `reelbot status --events 20` shows what happened, and the
error is stored on the row (`reelbot show ID`). `reelbot retry` re-queues
anything under `publish.max_attempts`.
