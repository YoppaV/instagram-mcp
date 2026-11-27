"""Per-account download manifest: what was already saved, and the bulk jobs.

One SQLite file per account, next to the media it describes::

    <INSTAGRAM_DOWNLOADS_DIR>/<account>/manifest.sqlite3

Tables:

* ``media`` — one row per media leaf, keyed by ``(pk, idx)`` (the post's
  numeric id and the 0-based position inside a carousel). ``status`` is
  ``done`` (file on disk), ``skipped`` (on purpose: a video that was not
  requested, too large...) or ``failed`` (retried on a later run, up to
  ``MAX_ATTEMPTS``).
* ``posts`` — one row per post seen, with how many leaves it has.
* ``source_posts`` — which source (``saved``, ``collection:<id>``,
  ``user:<name>``) listed which post. Files are shared between sources: a
  post that is both saved and on a profile is downloaded once.
* ``jobs`` — one row per bulk job (per source). Holds the resume cursor,
  the counters and the cancel flag, so a job survives a restart and can be
  stopped from another process.

The default rollback journal is used (no WAL): the downloads folder may live
on a network share, where WAL is unsafe. Every write commits at once.
Framework-free and synchronous; the operations are tiny.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MANIFEST_NAME = "manifest.sqlite3"
SCHEMA_VERSION = 1
# A leaf that failed this many times is given up (counts as known).
MAX_ATTEMPTS = 3

STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

REASON_VIDEO_NOT_REQUESTED = "video_not_requested"
# Skips that never change with options: the leaf is known for good.
_PERMANENT_SKIPS = ("too_large",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    pk          TEXT PRIMARY KEY,
    shortcode   TEXT NOT NULL,
    author      TEXT,
    taken_at    INTEGER,
    media_count INTEGER NOT NULL,
    first_seen  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    pk           TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    shortcode    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT,
    saved_path   TEXT,
    byte_size    INTEGER,
    sha256       TEXT,
    content_type TEXT,
    author       TEXT,
    taken_at     INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (pk, idx)
);
CREATE TABLE IF NOT EXISTS source_posts (
    source  TEXT NOT NULL,
    pk      TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (source, pk)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    target           TEXT,
    download_videos  INTEGER NOT NULL DEFAULT 0,
    mode             TEXT NOT NULL DEFAULT 'auto',
    status           TEXT NOT NULL,
    phase            TEXT,
    cursor           TEXT,
    full_done        INTEGER NOT NULL DEFAULT 0,
    known_stop       INTEGER NOT NULL,
    max_pages        INTEGER,
    max_posts        INTEGER,
    counters         TEXT NOT NULL DEFAULT '{}',
    error            TEXT,
    wait_until       REAL,
    owner            TEXT,
    heartbeat        REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    started_at       REAL,
    updated_at       REAL NOT NULL,
    finished_at      REAL
);
"""

_JOB_FIELDS = (
    "job_id", "source", "target", "download_videos", "mode", "status", "phase", "cursor",
    "full_done", "known_stop", "max_pages", "max_posts", "counters", "error", "wait_until",
    "owner", "heartbeat", "cancel_requested", "created_at", "started_at", "updated_at",
    "finished_at",
)


@dataclass(frozen=True)
class ManifestEntry:
    """One media leaf as the manifest records it."""

    account: str
    shortcode: str
    pk: str
    index: int
    kind: str
    status: str
    reason: str | None
    saved_path: str | None
    byte_size: int | None
    sha256: str | None
    author: str | None
    taken_at: int | None
    downloaded_at: float | None


def manifest_path(downloads_dir: Path, account: str) -> Path:
    return downloads_dir.expanduser() / account.casefold() / MANIFEST_NAME


class Manifest:
    """Open (and create) the manifest of one account."""

    def __init__(self, path: Path, *, account: str, clock=time.time) -> None:
        self.path = path
        self.account = account
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = self._connect(path)
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError):  # locked, disk full...
                raise
            # Corrupt file (e.g. power cut with synchronous=OFF): set it aside
            # and start over. Nothing is lost for good: files already on disk
            # are adopted by name on the next run, without re-downloading.
            aside = path.with_name(f"{path.name}.corrupt-{int(self._clock())}")
            os.replace(path, aside)
            self._db = self._connect(path)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            # No fsync per row: it costs ~10 ms each (far more on a network
            # share). A process crash loses nothing; an OS crash may at worst
            # corrupt the file, which is then rebuilt as above.
            db.execute("PRAGMA synchronous=OFF")
            db.executescript(_SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                       (str(SCHEMA_VERSION),))
            db.execute("SELECT COUNT(*) FROM jobs").fetchone()
        except BaseException:
            db.close()
            raise
        return db

    @classmethod
    def open_existing(cls, path: Path, *, account: str) -> "Manifest | None":
        return cls(path, account=account) if path.exists() else None

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # posts and media
    # ------------------------------------------------------------------
    def note_post(self, *, pk: str, shortcode: str, author: str | None,
                  taken_at: int | None, media_count: int, source: str) -> None:
        now = self._clock()
        self._db.execute(
            """INSERT INTO posts(pk, shortcode, author, taken_at, media_count, first_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pk) DO UPDATE SET shortcode=excluded.shortcode,
                   author=COALESCE(excluded.author, posts.author),
                   taken_at=COALESCE(excluded.taken_at, posts.taken_at),
                   media_count=excluded.media_count, updated_at=excluded.updated_at""",
            (pk, shortcode, author, taken_at, media_count, now, now),
        )
        self._db.execute(
            "INSERT OR IGNORE INTO source_posts(source, pk, seen_at) VALUES (?, ?, ?)",
            (source, pk, now),
        )

    def leaf(self, pk: str, idx: int) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM media WHERE pk=? AND idx=?", (pk, idx)
        ).fetchone()

    @staticmethod
    def leaf_is_known(row: sqlite3.Row | None, *, download_videos: bool) -> bool:
        """Nothing left to do for this leaf under these options."""
        if row is None:
            return False
        status = row["status"]
        if status == STATUS_DONE:
            return True
        if status == STATUS_SKIPPED:
            if row["reason"] == REASON_VIDEO_NOT_REQUESTED:
                return not download_videos
            return True
        if status == STATUS_FAILED:
            return row["reason"] in _PERMANENT_SKIPS or row["attempts"] >= MAX_ATTEMPTS
        return False

    def is_known(self, pk: str, media_count: int, *, download_videos: bool) -> bool:
        """Every leaf of the post is done (or given up) under these options."""
        if media_count <= 0:
            return self._db.execute("SELECT 1 FROM posts WHERE pk=?", (pk,)).fetchone() is not None
        rows = {
            r["idx"]: r
            for r in self._db.execute("SELECT * FROM media WHERE pk=?", (pk,)).fetchall()
        }
        return all(
            self.leaf_is_known(rows.get(i), download_videos=download_videos)
            for i in range(media_count)
        )

    def record(self, *, pk: str, idx: int, shortcode: str, kind: str, status: str,
               reason: str | None = None, saved_path: str | None = None,
               byte_size: int | None = None, sha256: str | None = None,
               content_type: str | None = None, author: str | None = None,
               taken_at: int | None = None) -> None:
        now = self._clock()
        bump = 1 if status == STATUS_FAILED else 0
        self._db.execute(
            """INSERT INTO media(pk, idx, shortcode, kind, status, reason, saved_path, byte_size,
                                 sha256, content_type, author, taken_at, attempts, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pk, idx) DO UPDATE SET shortcode=excluded.shortcode,
                   kind=excluded.kind, status=excluded.status, reason=excluded.reason,
                   saved_path=excluded.saved_path, byte_size=excluded.byte_size,
                   sha256=excluded.sha256, content_type=excluded.content_type,
                   author=COALESCE(excluded.author, media.author),
                   taken_at=COALESCE(excluded.taken_at, media.taken_at),
                   attempts=media.attempts + ?, updated_at=excluded.updated_at""",
            (pk, idx, shortcode, kind, status, reason, saved_path, byte_size, sha256,
             content_type, author, taken_at, bump, now, bump),
        )

    def entries(self, *, source: str | None = None) -> list[ManifestEntry]:
        if source is None:
            rows = self._db.execute("SELECT * FROM media ORDER BY pk, idx").fetchall()
        else:
            rows = self._db.execute(
                """SELECT m.* FROM media m JOIN source_posts s ON s.pk = m.pk
                   WHERE s.source=? ORDER BY m.pk, m.idx""", (source,)
            ).fetchall()
        return [
            ManifestEntry(
                account=self.account, shortcode=r["shortcode"], pk=r["pk"], index=r["idx"],
                kind=r["kind"], status=r["status"], reason=r["reason"],
                saved_path=r["saved_path"], byte_size=r["byte_size"], sha256=r["sha256"],
                author=r["author"], taken_at=r["taken_at"],
                downloaded_at=r["updated_at"] if r["status"] == STATUS_DONE else None,
            )
            for r in rows
        ]

    def totals(self, *, source: str | None = None) -> dict[str, int]:
        if source is None:
            rows = self._db.execute(
                "SELECT status, COUNT(*) n, COALESCE(SUM(byte_size), 0) b FROM media GROUP BY status"
            ).fetchall()
            posts = self._db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        else:
            rows = self._db.execute(
                """SELECT m.status, COUNT(*) n, COALESCE(SUM(m.byte_size), 0) b
                   FROM media m JOIN source_posts s ON s.pk = m.pk WHERE s.source=?
                   GROUP BY m.status""", (source,)
            ).fetchall()
            posts = self._db.execute(
                "SELECT COUNT(*) FROM source_posts WHERE source=?", (source,)
            ).fetchone()[0]
        out = {"posts": int(posts), "done": 0, "skipped": 0, "failed": 0, "bytes": 0}
        for r in rows:
            out[r["status"]] = int(r["n"])
            if r["status"] == STATUS_DONE:
                out["bytes"] = int(r["b"])
        return out

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return _job_dict(row) if row else None

    def jobs(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
        return [_job_dict(r) for r in rows]

    def create_job(self, job_id: str, *, source: str, target: str | None,
                   known_stop: int) -> dict[str, Any]:
        now = self._clock()
        self._db.execute(
            """INSERT OR IGNORE INTO jobs(job_id, source, target, status, known_stop,
                                          created_at, updated_at)
               VALUES (?, ?, ?, 'new', ?, ?, ?)""",
            (job_id, source, target, known_stop, now, now),
        )
        job = self.job(job_id)
        assert job is not None
        return job

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        bad = set(fields) - set(_JOB_FIELDS)
        if bad:
            raise KeyError(f"unknown job fields: {sorted(bad)}")
        if "counters" in fields and not isinstance(fields["counters"], str):
            fields["counters"] = json.dumps(fields["counters"], sort_keys=True)
        fields.setdefault("updated_at", self._clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._db.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))

    def request_cancel(self, job_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
            (self._clock(), job_id),
        )
        return cur.rowcount > 0

    def cancel_requested(self, job_id: str) -> bool:
        row = self._db.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return bool(row and row[0])


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    try:
        out["counters"] = json.loads(out.get("counters") or "{}")
    except ValueError:
        out["counters"] = {}
    for key in ("download_videos", "full_done", "cancel_requested"):
        out[key] = bool(out.get(key))
    return out


def iter_manifests(downloads_dir: Path, accounts: Iterable[str]) -> Iterable[tuple[str, Path]]:
    for account in accounts:
        path = manifest_path(downloads_dir, account)
        if path.exists():
            yield account, path
