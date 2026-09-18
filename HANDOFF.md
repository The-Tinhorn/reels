# reelbot — maintainer handoff

Written for whoever picks this up next, human or AI. It covers what the thing
is, why it is built the way it is, what has already gone wrong, and what is
still unproven. Read the **Invariants** and **Already fixed** sections before
changing anything — most of the non-obvious code exists because something broke.

Repository: `The-Tinhorn/reels`
Working branch: `claude/youtube-shorts-instagram-downloader-1lihx5` (not yet merged to `main`)
Image: `ghcr.io/the-tinhorn/reels:latest` (linux/amd64 + linux/arm64, public, no login to pull)
~3,000 lines of Python, 124 tests, all offline.

---

## 1. What it does

Watches a YouTube channel for new Shorts, queues them for a human to approve,
then downloads the approved ones with yt-dlp, converts them to Reels format
with ffmpeg, and posts them to Instagram.

```
discover ──> pending ──[human approves]──> approved ──> download ──> normalize ──> posted
                 │
                 └──[human rejects]──> rejected        (never downloaded, never posted)
```

It posts using the account owner's own Instagram login via `instagrapi`, which
speaks Instagram's private mobile API. **No Meta developer account, app, or API
token is required.** That was an explicit product requirement, not an accident —
see §4.

The owner is a content creator reposting **her own** YouTube Shorts as Reels.
The approval queue exists so the rights decision is made per video by a person.

---

## 2. Current deployment

Runs on a Raspberry Pi under **CasaOS**, installed from
`docker-compose.casaos.yml` via CasaOS's Custom Install.

- One container. The pipeline loops hourly *and* serves the approval web UI
  from the same process (`run --loop --with-review`).
- All mutable state lives in `/DATA/AppData/reelbot` on the host, mounted at
  `/data`: `config.yaml`, `.env`, `data/reelbot.db`, `data/downloads/`,
  `data/ig_session.json`.
- Every setting is an environment variable, editable in the CasaOS GUI. The
  container writes its own `config.yaml` on first start.
- Pushing to the branch rebuilds and republishes the image automatically
  (`.github/workflows/docker.yml`). Tests run on every push too.

**Status: not yet posting.** Discovery and approval work end to end. Downloads
are currently blocked by YouTube's "Sign in to confirm you're not a bot" check —
see §6.

---

## 3. Architecture

```
reelbot/
  config.py       YAML + ${ENV} loading, typed dataclasses, type coercion
  store.py        SQLite queue and state machine (thread-safe)
  discover.py     yt-dlp enumeration + filtering
  download.py     yt-dlp download (refuses unapproved videos)
  ytdlp.py        shared yt-dlp auth: cookie discovery, player_client
  media.py        ffprobe/ffmpeg normalization to Reels spec
  caption.py      caption templating
  publishers/
    base.py             Publisher protocol + PublishResult
    instagrapi_backend  private mobile API, no Meta keys  ← the default in production
    graph_backend       official Meta Graph API (needs keys, a Business account,
                        and a public HTTPS URL Meta can fetch the file from)
    dryrun              writes what it would post; the default in a fresh config
  pipeline.py     orchestration, rate limits, retries
  review.py       the local approval UI (stdlib http.server, HTTP Basic auth)
  cli.py          the command line
```

### Status lifecycle (`store.py`)

`pending → approved → downloaded → posted`, with `rejected` and `failed` off to
the side. `failed` is retryable up to `publish.max_attempts`.

Two auxiliary tables:

- `filtered` — video ids rejected by the discovery filters, so an hourly run
  does not re-fetch them forever. See §5 for why this is subtle.
- `meta` — currently just a fingerprint of the filter settings.
- `events` — an append-only log, surfaced by `reelbot status --events N`.

---

## 4. Invariants — do not break these

**1. Nothing is downloaded or posted without an approval record.**
`download.download_video` refuses anything whose status is not `approved`;
`pipeline.publish_video` refuses anything not `downloaded`. Both checks are
deliberate and both are tested. The approval writes `reviewed_by` and
`reviewed_at`. `auto_approve` is per-source and off by default — it exists only
for a channel whose content is the operator's own.

**2. `instagrapi` is the default, not the Graph API.** The whole point is
posting without Meta API keys. The Graph backend exists as an option; do not
promote it to default.

**3. The Instagram session is persisted and reused.**
`data/ig_session.json` holds the session *and the device fingerprint*. Logging
in fresh on every run is the fastest way to get an account action-blocked. If
the saved session expires, the code re-logs-in **keeping the same device
UUIDs**. Do not "simplify" this away.

**4. Rate limits are a safety feature.** `max_per_day`,
`min_minutes_between_posts`, `posting_hours`, and the random jitter in
`instagram.delay_range` exist to keep a real account from being flagged.
Defaults are deliberately conservative.

**5. The review UI refuses to bind to a non-loopback address without a
password.** It shows the whole queue and can approve posts. In a container it
must bind `0.0.0.0`, so the password is the only thing protecting it.
`--insecure` overrides, for someone who really means it.

**6. Files that already conform are uploaded untouched.** `media.conforms()`
checks codec, container, pixel format, aspect and duration; most Shorts pass
and skip ffmpeg entirely. This is what makes a Raspberry Pi viable.

---

## 5. Why the non-obvious code is like that

Each of these is a trap someone will otherwise walk into again.

**Config values arrive as strings.** Docker and CasaOS pass every setting as a
string, so `config.coerce()` converts to each dataclass field's declared type:
ints, bools, `Optional[int]` where empty means unset, and lists from
`"9,13,19"` or `"#reels #shorts"`. Without it, `max_per_day: "3"` silently
becomes a string and blows up on the first comparison. A bad value raises
`ConfigError` naming the setting.

**Templated YAML values are quoted.** `hashtags: ${REELBOT_HASHTAGS:-#reels #shorts}`
unquoted is truncated by YAML at the ` #` — it reads as a comment. Every
`${...}` value in `templates/config.example.yaml` is quoted for this reason.

**`.env` parsing is last-wins.** The shipped `.env` has empty placeholders. An
earlier implementation let the *first* occurrence of a key win, so an empty
placeholder shadowed a value appended below it. Real environment variables
still take precedence over the file.

**Global CLI flags use `argparse.SUPPRESS` + post-parse defaults.**
`parents=[common]` shares the *same action objects* between the main parser and
every subparser, so `set_defaults` on one clobbers all of them, and a subparser's
defaults overwrite what the main parser already parsed. `cli.parse_args()` fills
`GLOBAL_DEFAULTS` after parsing instead. This is why `reelbot -c x.yaml run`
and `reelbot run -c x.yaml` both work.

**SQLite connections are per-thread.** The review UI is a
`ThreadingHTTPServer`; a shared connection raises
`SQLite objects created in a thread can only be used in that same thread` on
every request. WAL mode plus a busy timeout also lets a second process (e.g.
`docker exec ... reelbot status`) read while the pipeline writes.

**The `filtered` table needs a fingerprint.** Remembering rejects stops an
hourly run re-fetching them, but if the filters *change*, everything they
previously rejected gets skipped before the new filter is ever applied — so
loosening a filter appeared to do nothing. `discover.sync_filters()` hashes the
filter settings and clears the table when the hash changes.

**The two `max_duration` settings must stay equal.** `filters.max_duration`
skips a video at discovery; `media.max_duration` is an ffmpeg `-t` that
*trims*. When only the filter was raised, a 2-minute Short passed discovery and
was silently cut to 90 seconds on the way out. They now share
`REELBOT_MAX_DURATION`, making the trim a safety net, and `normalize()` warns
when it actually cuts anything.

**The container starts as root, then drops privileges.** CasaOS and
`docker run -v` both create the host volume as root, so an image that runs as
UID 1000 outright cannot write its own config. `docker-entrypoint.sh` chowns
`/data` to `PUID:PGID` (1000:1000 default) and `exec`s through `gosu`. The
chown only fires when ownership is actually wrong, so it does not walk a large
downloads directory every start.

**Cleanup globs `<video id>.*`.** A posted video leaves a download, a
normalized copy, a cover frame and a thumbnail. Deleting only the two paths the
database knows about left the rest behind, so the directory
`delete_after_post` was meant to keep empty grew anyway.

**Encoding cost is dominated by the blur, not the preset.** Measured on one 20s
clip: blur + `veryfast` 16.6s, blur + `slow` 20.3s, black bars + `veryfast`
6.0s. On a slow machine, change `media.background` to `black` before touching
`media.preset`.

---

## 6. Open problems

**YouTube bot check (live blocker).** Downloads fail with
`Sign in to confirm you're not a bot`. Mitigations shipped, none confirmed
working yet on the owner's Pi:

- `cookies.txt` beside `config.yaml` is picked up automatically (also
  `youtube-cookies.txt`, `youtube.com_cookies.txt`). Must be Netscape format,
  exported from a private window logged into a **throwaway** account, and that
  window closed *without logging out* or the cookies are invalidated. They
  expire every few weeks.
- `REELBOT_PLAYER_CLIENT=tv` (or `web_safari`) impersonates a different YouTube
  client and sometimes sidesteps the check with no account at all. Try this
  first.
- Keeping yt-dlp current matters; the image installs the latest at build time,
  so rebuilding is a real fix for extractor breakage.

If neither works, the next things to try are a different `player_client`
combination, slowing downloads (`download.rate_limit`), or routing through a
residential proxy.

**Nothing has ever been posted to Instagram.** The `instagrapi` backend has
never run against a real account. Expect first-run surprises: a login
challenge, a 2FA prompt, or `clip_upload` rejecting a file. `reelbot login`
exercises auth alone and is the right first test.

**The Graph backend has never been run against the real API.** It is written
from the documented shapes; treat it as unverified.

**No live YouTube fetch has ever been tested.** The development sandbox blocked
youtube.com by network policy, so every test fakes the yt-dlp call. Everything
downstream of that call — real ffmpeg re-encodes, the real review server — is
exercised for real.

**Config files are never migrated.** `docker-entrypoint.sh` only writes
`config.yaml` if it is absent, so an existing install does not pick up new
options added to the template. Workaround: rename `config.yaml` and restart to
regenerate it. Nothing is lost, because the queue lives in the database and
everything else comes from environment variables. A real migration step would
be a genuine improvement.

---

## 7. Working on it

```bash
pip install -r requirements.txt pytest
pytest -q                      # 124 tests, offline, ~17s
python -m pyflakes reelbot tests
```

`tests/test_integration.py` drives the whole pipeline with only the yt-dlp
network call faked — real ffmpeg, real review server. ffmpeg-dependent tests
skip themselves if it is missing, so **check they are not silently skipping**
before trusting a green run.

Release: push to the branch. `.github/workflows/docker.yml` builds
linux/amd64 + linux/arm64 and pushes `latest`, `sha-<short>`, and a
branch-named tag to GHCR. Takes 2–6 minutes. The Pi picks it up via CasaOS's
⋮ → Update, or `docker pull` + restart.

Verify a published image without Docker:

```bash
TOK=$(curl -s "https://ghcr.io/token?scope=repository:the-tinhorn/reels:pull&service=ghcr.io" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -sI -H "Authorization: Bearer $TOK" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://ghcr.io/v2/the-tinhorn/reels/manifests/latest | grep -i docker-content-digest
```

---

## 8. Operating it

```bash
docker exec -u 1000 reelbot reelbot status --events 20   # queue, budget, recent activity
docker exec -u 1000 reelbot reelbot discover -v          # a discovery pass now
docker exec -u 1000 reelbot reelbot discover --refilter  # re-check what filters rejected
docker exec -u 1000 reelbot reelbot list --status failed
docker exec -u 1000 reelbot reelbot show <video-id>      # everything known + caption preview
docker exec -u 1000 reelbot reelbot retry                # re-queue failures with attempts left
docker exec -u 1000 reelbot reelbot login                # Instagram auth only
docker logs reelbot --tail 50
```

`-u 1000` matters: `docker exec` bypasses the entrypoint and would otherwise
run as root, creating root-owned files the main process cannot overwrite.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `REELBOT_SOURCE_URL` | — | Channel/playlist/search URL to watch |
| `REELBOT_SOURCE_LIMIT` | `20` | How many videos back to enumerate |
| `REELBOT_BACKEND` | `dryrun` | `dryrun` posts nothing; `instagrapi` posts for real |
| `REELBOT_MAX_PER_DAY` | `3` | Posts per 24h. Empty = unlimited, `0` = paused |
| `REELBOT_MIN_MINUTES_BETWEEN_POSTS` | `90` | Minimum gap |
| `REELBOT_POSTING_HOURS` | any | e.g. `9,13,19`, local time |
| `REELBOT_MAX_DURATION` | `180` | Skip longer videos; also the trim ceiling |
| `REELBOT_PUBLISHED_WITHIN_DAYS` | none | Age limit. Empty = no limit |
| `REELBOT_HASHTAGS` | `#reels #shorts` | Appended to captions |
| `REELBOT_PRESET` | `medium` | x264 preset; `veryfast` on a Pi |
| `REELBOT_DELETE_AFTER_POST` | `false` | Free disk once a Reel is up |
| `REELBOT_COOKIES_FILE` | auto | `cookies.txt` beside the config is found anyway |
| `REELBOT_PLAYER_CLIENT` | yt-dlp default | e.g. `tv` — route past the bot check |
| `REELBOT_REVIEW_PASSWORD` | — | Required for a non-localhost review UI |
| `IG_USERNAME` / `IG_PASSWORD` | — | Instagram login |
| `IG_TOTP_SEED` | — | 2FA **seed key**, not a 6-digit code |
| `PUID` / `PGID` | `1000` | Who owns the files it writes |
| `TZ` | `UTC` | So `posting_hours` means local time |

Anything not listed is in `config.yaml`, which is the full configuration;
environment variables only fill in its placeholders.

---

## 9. If you are an AI picking this up

- Read §4 and §5 first. Most of the odd-looking code is load-bearing and was
  written in response to a specific failure.
- The approval gate is the product, not a formality. Do not add a flag that
  posts without review, and do not let `auto_approve` become the default.
- Do not weaken the rate limits, the session persistence, or the review
  password check to make something easier to test.
- Run `pytest` before and after. If you change behaviour, add a test that
  fails without the change — that is how every bug in §5 was caught.
- Be careful claiming something works. Large parts of this have never touched
  the real YouTube or Instagram (§6). Say which parts you actually verified.
