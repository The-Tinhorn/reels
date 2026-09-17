"""SQLite-backed queue of candidate videos and their approval/publish state.

Status lifecycle::

    pending ──approve──> approved ──download──> downloaded ──publish──> posted
       │                                             │
       └──reject──> rejected                         └──> failed (retryable)

Nothing is ever published unless it passed through ``approved``, and the row
records who approved it and when.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
DOWNLOADED = "downloaded"
POSTED = "posted"
FAILED = "failed"

ALL_STATUSES = (PENDING, APPROVED, REJECTED, DOWNLOADED, POSTED, FAILED)
#: Statuses a video can be published from.
PUBLISHABLE = (DOWNLOADED,)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT,
    description     TEXT,
    channel         TEXT,
    channel_url     TEXT,
    duration        INTEGER,
    view_count      INTEGER,
    like_count      INTEGER,
    upload_date     TEXT,
    thumbnail_url   TEXT,
    source          TEXT,
    status          TEXT NOT NULL,
    caption         TEXT,
    video_path      TEXT,
    thumb_path      TEXT,
    ig_media_id     TEXT,
    ig_permalink    TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    discovered_at   TEXT,
    reviewed_at     TEXT,
    reviewed_by     TEXT,
    downloaded_at   TEXT,
    posted_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_posted_at ON videos(posted_at);

-- Videos that failed the discovery filters. Remembered so a scheduled run
-- does not re-fetch and re-evaluate the same rejects every hour.
CREATE TABLE IF NOT EXISTS filtered (
    id      TEXT PRIMARY KEY,
    reason  TEXT,
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id  TEXT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_video ON events(video_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Video:
    id: str
    url: str
    title: str = ""
    description: str = ""
    channel: str = ""
    channel_url: str = ""
    duration: int = 0
    view_count: int = 0
    like_count: int = 0
    upload_date: str = ""
    thumbnail_url: str = ""
    source: str = ""
    status: str = PENDING
    caption: Optional[str] = None
    video_path: Optional[str] = None
    thumb_path: Optional[str] = None
    ig_media_id: Optional[str] = None
    ig_permalink: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    discovered_at: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    downloaded_at: Optional[str] = None
    posted_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Video":
        return cls(**{k: row[k] for k in row.keys() if k in cls.__annotations__})

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class Store:
    """Queue state in SQLite.

    A connection is opened per thread, because the review UI serves requests on
    worker threads and SQLite connections cannot cross threads. WAL mode plus a
    busy timeout keeps a concurrent reader (the UI) and writer (the pipeline)
    from tripping over each other.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                # A connection belonging to a worker thread closes with that
                # thread; nothing useful to do here.
                pass
        self._local = threading.local()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ------------------------------------------------------------------ writes

    def add_candidate(self, video: Video) -> bool:
        """Insert a newly discovered video. Returns False if already known."""
        if self.get(video.id):
            return False
        video.discovered_at = video.discovered_at or utcnow()
        columns = list(video.__dict__.keys())
        placeholders = ", ".join("?" for _ in columns)
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO videos ({', '.join(columns)}) VALUES ({placeholders})",
                [getattr(video, c) for c in columns],
            )
        self.log(video.id, "discovered", video.source)
        return True

    def update(self, video_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE videos SET {assignments} WHERE id = ?",
                [*fields.values(), video_id],
            )

    def set_status(self, video_id: str, status: str, **fields: Any) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status: {status}")
        self.update(video_id, status=status, **fields)
        self.log(video_id, f"status:{status}", fields.get("error"))

    def approve(self, video_id: str, by: str = "cli", caption: Optional[str] = None) -> bool:
        video = self.get(video_id)
        if video is None or video.status not in (PENDING, REJECTED):
            return False
        fields: Dict[str, Any] = {"reviewed_at": utcnow(), "reviewed_by": by, "error": None}
        if caption is not None:
            fields["caption"] = caption
        self.set_status(video_id, APPROVED, **fields)
        return True

    def reject(self, video_id: str, by: str = "cli") -> bool:
        video = self.get(video_id)
        if video is None or video.status in (POSTED, DOWNLOADED):
            return False
        self.set_status(video_id, REJECTED, reviewed_at=utcnow(), reviewed_by=by)
        return True

    def mark_filtered(self, video_id: str, reason: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO filtered (id, reason, ts) VALUES (?, ?, ?)",
                (video_id, reason, utcnow()),
            )

    def is_filtered(self, video_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM filtered WHERE id = ?", (video_id,)
        ).fetchone()
        return row is not None

    def clear_filtered(self) -> int:
        """Forget every filtered video so changed filters get a fresh look."""
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM filtered")
            return cursor.rowcount

    def log(self, video_id: Optional[str], kind: str, detail: Any = None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (video_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                (video_id, utcnow(), kind, detail),
            )

    # ------------------------------------------------------------------- reads

    def get(self, video_id: str) -> Optional[Video]:
        row = self.conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return Video.from_row(row) if row else None

    def list(
        self,
        status: Optional[str | Iterable[str]] = None,
        limit: Optional[int] = None,
        order: str = "discovered_at ASC",
    ) -> List[Video]:
        query = "SELECT * FROM videos"
        params: List[Any] = []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            query += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        query += f" ORDER BY {order}"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        return [Video.from_row(r) for r in self.conn.execute(query, params)]

    def counts(self) -> Dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM videos GROUP BY status")
        counts = {status: 0 for status in ALL_STATUSES}
        counts.update({r["status"]: r["c"] for r in rows})
        return counts

    def posts_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE status = ? AND posted_at >= ?",
            (POSTED, since.isoformat(timespec="seconds")),
        ).fetchone()
        return int(row["c"])

    def posts_today(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        return self.posts_since(now - timedelta(days=1))

    def last_post_time(self) -> Optional[datetime]:
        row = self.conn.execute(
            "SELECT MAX(posted_at) t FROM videos WHERE status = ?", (POSTED,)
        ).fetchone()
        return parse_ts(row["t"]) if row and row["t"] else None

    def recent_events(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        )
