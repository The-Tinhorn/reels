"""End-to-end: discover -> approve -> download -> normalize -> publish.

The only thing faked is the yt-dlp network call (this suite must run offline).
Everything after it is the real code path, including a real ffmpeg re-encode
and the real review server.
"""

import json
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import make_video
from reelbot import discover, download, media, pipeline, review
from reelbot.publishers import get_publisher
from reelbot.store import APPROVED, DOWNLOADED, PENDING, POSTED, REJECTED

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")

ENTRIES = [
    {"id": "short1", "title": "Cats being dramatic", "duration": 21, "view_count": 84000,
     "channel": "Test Channel", "url": "short1"},
    {"id": "short2", "title": "Sponsored #ad read", "duration": 25, "view_count": 12,
     "channel": "Test Channel", "url": "short2"},
    {"id": "short3", "title": "Way too long", "duration": 900, "view_count": 500000,
     "channel": "Test Channel", "url": "short3"},
]


def fake_extract(url, opts):
    if url.startswith("https://example.invalid"):
        return {"entries": ENTRIES}
    return next((e for e in ENTRIES if e["id"] in url), None)


def make_fake_downloader(width=1080, height=1920, seconds=2):
    """Stands in for yt-dlp: writes a real (landscape or vertical) video file."""

    def downloader(url, opts):
        video_id = next(e["id"] for e in ENTRIES if e["id"] in url)
        outdir = Path(opts["outtmpl"]).parent
        outdir.mkdir(parents=True, exist_ok=True)
        dest = outdir / f"{video_id}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30:duration={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(dest)],
            check=True, capture_output=True,
        )
        return {"duration": seconds, "view_count": 84000, "title": "Cats being dramatic"}

    return downloader


@needs_ffmpeg
def test_full_pipeline(cfg, store):
    cfg.filters.title_deny = ["#ad"]
    cfg.filters.max_duration = 90
    cfg.media.target_width, cfg.media.target_height, cfg.media.crf = 540, 960, 32
    cfg.caption.template = "{title}\n\nvia {channel}\n{hashtags}"
    cfg.caption.hashtags = ["#reels", "#cats"]

    # 1. discovery queues only what passes the filters, and only as `pending`
    added, skipped = discover.discover_all(cfg, store, extractor=fake_extract)
    assert (added, skipped) == (1, 2)          # #ad and the 900s video dropped
    assert store.get("short1").status == PENDING

    # 2. nothing downloads while it is unreviewed
    assert download.download_approved(cfg, store, downloader=make_fake_downloader()) == []

    # 3. a human approves it
    assert store.approve("short1", by="tester") is True
    assert store.get("short1").status == APPROVED

    # 4. download (landscape source, so normalization has real work to do)
    got = download.download_approved(cfg, store, downloader=make_fake_downloader(640, 480))
    assert len(got) == 1
    video = store.get("short1")
    assert video.status == DOWNLOADED and Path(video.video_path).exists()

    # 5. publish through the dryrun backend
    results = pipeline.publish_due(cfg, store, limit=1, publisher=get_publisher(cfg))
    assert len(results) == 1

    posted = store.get("short1")
    assert posted.status == POSTED and posted.posted_at
    assert posted.caption == "Cats being dramatic\n\nvia Test Channel\n#reels #cats"

    # the file that actually went out is Reels-shaped
    record_path = cfg.resolve("data/dryrun") / f"{results[0].media_id}.json"
    record = json.loads(record_path.read_text())
    uploaded = Path(record["video"])
    assert uploaded.name == "short1.reel.mp4"
    info = media.probe(uploaded, cfg.media)
    assert (info.width, info.height) == (540, 960)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert record["caption"] == posted.caption
    assert Path(record["thumbnail"]).exists()

    # 6. the budget now blocks a second post
    allowed, reason = pipeline.can_post_now(cfg, store)
    assert allowed is False and "too soon" in reason


@needs_ffmpeg
def test_already_vertical_video_is_not_re_encoded(cfg, store):
    cfg.media.target_width, cfg.media.target_height, cfg.media.crf = 540, 960, 32
    store.add_candidate(make_video("short1", title="Cats being dramatic"))
    store.approve("short1")
    download.download_approved(cfg, store, downloader=make_fake_downloader(540, 960))

    source = Path(store.get("short1").video_path)
    assert media.prepare(source, cfg) == source      # passed straight through
    assert not source.with_name("short1.reel.mp4").exists()


# ----------------------------------------------------------------- review UI


class ReviewServer:
    def __init__(self, cfg, store):
        handler = type("H", (review.ReviewHandler,), {"cfg": cfg, "store": store})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path="/"):
        with urllib.request.urlopen(self.url + path, timeout=10) as resp:
            return resp.status, resp.read().decode()

    def post(self, **form):
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(self.url + "/action", data=data, method="POST")
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status


def test_review_ui_approves_and_rejects(cfg, store):
    store.add_candidate(make_video("short1", title="Cats being dramatic"))
    store.add_candidate(make_video("short2", title="Nope"))

    with ReviewServer(cfg, store) as server:
        status, page = server.get("/")
        assert status == 200
        assert "Cats being dramatic" in page
        assert 'src="https://www.youtube.com/embed/short1"' in page   # preview
        assert "#reels" in page                                       # caption preview

        assert server.post(id="short1", action="approve", caption="my edited caption") == 200
        assert server.post(id="short2", action="reject") == 200

    approved = store.get("short1")
    assert approved.status == APPROVED
    assert approved.caption == "my edited caption" and approved.reviewed_by == "web"
    assert store.get("short2").status == REJECTED


def test_review_ui_escapes_html_in_titles(cfg, store):
    store.add_candidate(make_video("short1", title="<script>alert(1)</script>"))
    with ReviewServer(cfg, store) as server:
        _, page = server.get("/")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_review_ui_json_endpoint(cfg, store):
    store.add_candidate(make_video("short1"))
    with ReviewServer(cfg, store) as server:
        status, body = server.get("/api/queue")
    assert status == 200
    assert json.loads(body)[0]["id"] == "short1"


def test_review_ui_will_not_serve_files_outside_the_download_dir(cfg, store, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password123")
    store.add_candidate(make_video("short1"))
    store.update("short1", video_path=str(secret))

    with ReviewServer(cfg, store) as server:
        try:
            server.get("/media/short1")
            raise AssertionError("expected the request to be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403


# ------------------------------------------------- review UI authentication


def _auth_header(password: str, user: str = "reelbot") -> dict:
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class AuthedReviewServer(ReviewServer):
    def __init__(self, cfg, store, password):
        handler = type(
            "H", (review.ReviewHandler,), {"cfg": cfg, "store": store, "password": password}
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def get(self, path="/", headers=None):
        request = urllib.request.Request(self.url + path, headers=headers or {})
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read().decode()

    def post(self, headers=None, **form):
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            self.url + "/action", data=data, method="POST", headers=headers or {}
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status


def test_review_ui_rejects_a_missing_password(cfg, store):
    store.add_candidate(make_video("short1"))
    with AuthedReviewServer(cfg, store, "hunter2") as server:
        with pytest.raises(urllib.error.HTTPError) as caught:
            server.get("/")
    assert caught.value.code == 401
    assert "Basic" in caught.value.headers.get("WWW-Authenticate", "")


def test_review_ui_rejects_a_wrong_password(cfg, store):
    store.add_candidate(make_video("short1"))
    with AuthedReviewServer(cfg, store, "hunter2") as server:
        with pytest.raises(urllib.error.HTTPError) as caught:
            server.get("/", headers=_auth_header("wrong"))
    assert caught.value.code == 401


def test_review_ui_accepts_the_right_password(cfg, store):
    store.add_candidate(make_video("short1", title="Cats being dramatic"))
    with AuthedReviewServer(cfg, store, "hunter2") as server:
        status, page = server.get("/", headers=_auth_header("hunter2"))
        assert status == 200 and "Cats being dramatic" in page
        assert server.post(headers=_auth_header("hunter2"),
                           id="short1", action="approve", caption="ok") == 200
    assert store.get("short1").status == APPROVED


def test_review_ui_posts_need_the_password_too(cfg, store):
    """A 401 on GET is no use if POST /action is open."""
    store.add_candidate(make_video("short1"))
    with AuthedReviewServer(cfg, store, "hunter2") as server:
        with pytest.raises(urllib.error.HTTPError) as caught:
            server.post(id="short1", action="approve")
    assert caught.value.code == 401
    assert store.get("short1").status == PENDING      # unchanged


def test_serve_refuses_a_public_bind_without_a_password(cfg, store):
    cfg.review.password = ""
    with pytest.raises(review.ReviewError, match="without a password"):
        review.serve(cfg, store, host="0.0.0.0", open_browser=False)


def test_is_loopback():
    assert review.is_loopback("127.0.0.1") is True
    assert review.is_loopback("localhost") is True
    assert review.is_loopback("::1") is True
    assert review.is_loopback("0.0.0.0") is False
    assert review.is_loopback("192.168.1.50") is False
