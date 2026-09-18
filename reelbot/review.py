"""A small local web UI for approving or rejecting queued Shorts.

Deliberately stdlib-only (no Flask) and bound to 127.0.0.1 by default: it is a
review console for one person on one machine, not a public service.
"""

from __future__ import annotations

import base64
import hmac
import html
import ipaddress
import json
import logging
import mimetypes
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from reelbot.caption import build_caption
from reelbot.config import Config
from reelbot.store import ALL_STATUSES, APPROVED, PENDING, POSTED, Store

log = logging.getLogger(__name__)


class ReviewError(Exception):
    pass


def is_loopback(host: str) -> bool:
    """True for addresses only reachable from this machine."""
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>reelbot review</title>
<style>
  :root {{
    --bg: #f6f7f9; --card: #fff; --fg: #16181d; --muted: #6b7280;
    --line: #e4e6eb; --accent: #2563eb; --ok: #15803d; --no: #b91c1c;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1115; --card: #181b21; --fg: #e8eaed; --muted: #9aa0a6;
      --line: #2a2e37; --accent: #60a5fa; --ok: #4ade80; --no: #f87171;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--fg);
         font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  header {{ position: sticky; top: 0; z-index: 10; background: var(--card);
            border-bottom: 1px solid var(--line); padding: 14px 16px;
            display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
  h1 {{ font-size: 17px; margin: 0; font-weight: 650; }}
  nav a {{ color: var(--muted); text-decoration: none; margin-right: 12px; font-size: 14px; }}
  nav a.on {{ color: var(--accent); font-weight: 600; }}
  main {{ max-width: 980px; margin: 0 auto; padding: 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
           padding: 16px; margin-bottom: 16px; display: grid;
           grid-template-columns: 220px 1fr; gap: 16px; }}
  @media (max-width: 680px) {{ .card {{ grid-template-columns: 1fr; }} }}
  .media iframe, .media video, .media img {{ width: 100%; aspect-ratio: 9/16;
           border: 0; border-radius: 8px; background: #000; display: block; }}
  .title {{ font-weight: 600; margin: 0 0 6px; }}
  .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; }}
  .meta a {{ color: var(--accent); }}
  textarea {{ width: 100%; min-height: 120px; padding: 10px; border-radius: 8px;
              border: 1px solid var(--line); background: var(--bg); color: var(--fg);
              font: inherit; font-size: 14px; resize: vertical; }}
  .actions {{ display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }}
  button {{ font: inherit; font-weight: 600; padding: 9px 16px; border-radius: 8px;
            border: 1px solid var(--line); cursor: pointer; background: var(--card);
            color: var(--fg); }}
  button.approve {{ background: var(--ok); border-color: var(--ok); color: #fff; }}
  button.reject {{ background: transparent; color: var(--no); border-color: var(--no); }}
  .empty {{ text-align: center; color: var(--muted); padding: 60px 20px; }}
  .pill {{ display: inline-block; font-size: 12px; padding: 2px 9px; border-radius: 999px;
           border: 1px solid var(--line); color: var(--muted); }}
  .counts {{ margin-left: auto; color: var(--muted); font-size: 13px; }}
</style>
</head>
<body>
<header>
  <h1>reelbot</h1>
  <nav>{nav}</nav>
  <span class="counts">{counts}</span>
</header>
<main>{body}</main>
</body>
</html>"""


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}" if minutes else f"{secs}s"


def _card(video, cfg: Config) -> str:
    e = html.escape
    caption = video.caption or build_caption(video, cfg.caption)

    if video.status in (PENDING, APPROVED) and not video.video_path:
        media = f'<iframe src="https://www.youtube.com/embed/{e(video.id)}" allowfullscreen loading="lazy"></iframe>'
    elif video.video_path and Path(video.video_path).exists():
        media = f'<video controls preload="metadata" src="/media/{e(video.id)}"></video>'
    elif video.thumbnail_url:
        media = f'<img src="{e(video.thumbnail_url)}" alt="">'
    else:
        media = '<div class="empty">no preview</div>'

    bits = [f'<span class="pill">{e(video.status)}</span>']
    if video.duration:
        bits.append(_fmt_duration(video.duration))
    if video.view_count:
        bits.append(f"{video.view_count:,} views")
    if video.upload_date:
        bits.append(e(video.upload_date))
    if video.source:
        bits.append(f"source: {e(video.source)}")

    links = [f'<a href="{e(video.url)}" target="_blank" rel="noopener">YouTube</a>']
    if video.ig_permalink:
        links.append(f'<a href="{e(video.ig_permalink)}" target="_blank" rel="noopener">Instagram</a>')

    if video.status == POSTED:
        actions = ""
    elif video.status == APPROVED:
        actions = """
        <div class="actions">
          <button class="reject" name="action" value="reject">Unapprove</button>
          <button name="action" value="save">Save caption</button>
        </div>"""
    else:
        actions = """
        <div class="actions">
          <button class="approve" name="action" value="approve">Approve</button>
          <button class="reject" name="action" value="reject">Reject</button>
        </div>"""

    error = f'<p class="meta" style="color:var(--no)">{e(video.error)}</p>' if video.error else ""

    return f"""
    <form class="card" method="post" action="/action">
      <input type="hidden" name="id" value="{e(video.id)}">
      <div class="media">{media}</div>
      <div>
        <p class="title">{e(video.title or video.id)}</p>
        <p class="meta">{e(video.channel)} &middot; {" &middot; ".join(bits)} &middot; {" &middot; ".join(links)}</p>
        {error}
        <textarea name="caption" spellcheck="true">{e(caption)}</textarea>
        {actions}
      </div>
    </form>"""


class ReviewHandler(BaseHTTPRequestHandler):
    cfg: Config
    store: Store
    password: str = ""       # empty disables authentication
    server_version = "reelbot"

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --------------------------------------------------------------------- auth

    def _authorized(self) -> bool:
        """HTTP Basic, so the browser handles the prompt. Any username works."""
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, supplied = decoded.partition(":")
        return hmac.compare_digest(supplied, self.password)

    def _challenge(self) -> None:
        body = b"authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="reelbot", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ replies

    def _send(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _render(self, status_filter: str) -> bytes:
        counts = self.store.counts()
        videos = self.store.list(
            status=None if status_filter == "all" else status_filter,
            order="discovered_at DESC",
            limit=200,
        )
        nav = " ".join(
            f'<a class="{"on" if s == status_filter else ""}" href="/?status={s}">{s}'
            f'{f" ({counts.get(s, 0)})" if s != "all" else ""}</a>'
            for s in ("pending", "approved", "downloaded", "posted", "rejected", "failed", "all")
        )
        body = "".join(_card(v, self.cfg) for v in videos) or (
            '<p class="empty">Nothing here. Run <code>reelbot discover</code> to queue candidates.</p>'
        )
        summary = " &middot; ".join(f"{k}: {v}" for k, v in counts.items() if v)
        return PAGE.format(nav=nav, body=body, counts=summary or "empty queue").encode("utf-8")

    # ------------------------------------------------------------------ routing

    def do_GET(self) -> None:
        if not self._authorized():
            self._challenge()
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            query = urllib.parse.parse_qs(parsed.query)
            status = (query.get("status") or ["pending"])[0]
            if status not in (*ALL_STATUSES, "all"):
                status = "pending"
            self._send(self._render(status))
        elif parsed.path.startswith("/media/"):
            self._serve_media(parsed.path.rsplit("/", 1)[-1])
        elif parsed.path == "/api/queue":
            data = [v.to_dict() for v in self.store.list(order="discovered_at DESC")]
            self._send(json.dumps(data, indent=2).encode(), "application/json")
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)

    def _serve_media(self, video_id: str) -> None:
        video = self.store.get(video_id)
        if not video or not video.video_path:
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        path = Path(video.video_path)
        # Only ever serve files this bot downloaded into its own directory.
        download_dir = self.cfg.resolve(self.cfg.download.dir).resolve()
        try:
            resolved = path.resolve()
            resolved.relative_to(download_dir)
        except (ValueError, OSError):
            self._send(b"forbidden", "text/plain; charset=utf-8", 403)
            return
        if not resolved.exists():
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self._send(resolved.read_bytes(), ctype)

    def do_POST(self) -> None:
        if not self._authorized():
            self._challenge()
            return
        if urllib.parse.urlparse(self.path).path != "/action":
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        video_id = (form.get("id") or [""])[0]
        action = (form.get("action") or [""])[0]
        caption = (form.get("caption") or [""])[0].strip()

        video = self.store.get(video_id)
        if video is None:
            self._redirect("/?status=pending")
            return

        if action == "approve":
            self.store.approve(video_id, by="web", caption=caption or None)
            log.info("approved %s via web UI", video_id)
        elif action == "reject":
            self.store.reject(video_id, by="web")
            log.info("rejected %s via web UI", video_id)
        elif action == "save":
            self.store.update(video_id, caption=caption or None)

        referer = self.headers.get("Referer") or "/?status=pending"
        self._redirect(referer)


def build_server(
    cfg: Config,
    store: Store,
    host: Optional[str] = None,
    port: Optional[int] = None,
    password: Optional[str] = None,
    allow_insecure: bool = False,
) -> ThreadingHTTPServer:
    """Construct the review server without starting it."""
    host = host or cfg.review.host
    port = port if port is not None else cfg.review.port
    password = cfg.review.password if password is None else password

    # The UI shows the whole queue and can approve posts. Off this machine,
    # that needs a password — in a container especially, where binding to
    # 0.0.0.0 is the only way to reach it at all.
    if not is_loopback(host) and not password and not allow_insecure:
        raise ReviewError(
            f"refusing to serve the review UI on {host} without a password.\n"
            "Set review.password in config.yaml (or REELBOT_REVIEW_PASSWORD in "
            ".env), or pass --insecure if this machine is genuinely private."
        )

    handler = type(
        "BoundReviewHandler",
        (ReviewHandler,),
        {"cfg": cfg, "store": store, "password": password},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.reelbot_url = f"http://{host}:{port}/"
    httpd.reelbot_password = password
    return httpd


def serve_in_background(
    cfg: Config,
    store: Store,
    host: Optional[str] = None,
    port: Optional[int] = None,
    password: Optional[str] = None,
    allow_insecure: bool = False,
) -> ThreadingHTTPServer:
    """Run the review UI on a daemon thread, alongside the pipeline loop.

    One container can then serve the approval queue and keep posting, which is
    how the single-service Docker/CasaOS deployment works.
    """
    httpd = build_server(cfg, store, host, port, password, allow_insecure)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="reelbot-review")
    thread.start()
    log.info("review UI listening on %s", httpd.reelbot_url)
    return httpd


def serve(
    cfg: Config,
    store: Store,
    host: Optional[str] = None,
    port: Optional[int] = None,
    open_browser: bool = True,
    password: Optional[str] = None,
    allow_insecure: bool = False,
) -> None:
    httpd = build_server(cfg, store, host, port, password, allow_insecure)
    url = httpd.reelbot_url
    print(f"Review UI at {url}  (Ctrl-C to stop)")
    if httpd.reelbot_password:
        print("Password protected — any username, the password from your config.")
    elif not is_loopback(httpd.server_address[0]):
        print("WARNING: no password set; anyone who can reach this port can post.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # headless machines have no browser to open
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
