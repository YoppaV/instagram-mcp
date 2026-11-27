"""Bulk downloads: a whole saved collection, all saved posts, or a profile grid.

A first full download does not fit in one MCP call (hundreds of pages, the
hourly budget), so it runs as a **background job** in small batches:

* one batch = one ``Runtime.run`` call = one feed page (21 saved / 12 per
  collection or grid page). The navigation lock is held only while that page
  is read; the media bytes are then streamed from the CDN **outside** the
  lock (``Runtime.download_semaphore``), so interactive tools interleave.
* the media URLs come from the feed payload itself — no navigation per post.
  Only a video whose feed node carries no ``video_versions`` (and only with
  ``download_videos``) costs one post-page navigation, at most
  ``FALLBACK_PER_BATCH`` per batch.
* between batches the job pauses ``BATCH_PAUSE_S`` and, before each batch,
  checks the account's hourly/daily budget: it leaves ``HOURLY_RESERVE`` /
  ``DAILY_RESERVE`` requests free for interactive use and waits (status
  ``waiting``) when it would eat into them. It never loosens the pacer.
* any Instagram signal (block, challenge, expired session) ends the job with
  ``status="error"``; nothing retries after a signal. So does an unusable
  state directory (``StateUnavailableError``: the runtime fails closed).
* if the budget still runs out (``BudgetExceededError``: other tools used
  it), the job stops as ``partial`` with the resume cursor kept and
  ``retry_after_s`` in ``error``; start it again later to continue.

What was already saved is in the per-account manifest (``manifest.py``,
keyed by post ``pk`` + media index), shared by every source. Walks go newest
to oldest:

* ``mode="auto"`` (default): the first run walks the whole source, saving the
  resume cursor after every page. A later run first scans the head and stops
  after ``known_stop`` consecutive known posts (incremental), then, if the
  first full walk never finished, resumes it from the saved cursor.
* ``mode="incremental"``: only the head scan.
* ``mode="full"``: walk everything again from the top (known posts are still
  not re-downloaded).

``max_pages`` / ``max_posts`` cap one run; the job then ends ``partial`` and
the next start resumes. A job is resumable after a crash or restart: start
it again with the same source. One job per account runs at a time
(``<downloads>/<account>/.bulk.lock``, held across processes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import downloader
from .accounts import DAY_S, HOUR_S
from .cursors import STOP_BUDGET, CursorState, PageResult, paginate
from .endpoints import media as media_ep
from .endpoints import saved
from .endpoints._ids import normalize_username
from .errors import (
    AccountBlockedError,
    AccountError,
    BudgetExceededError,
    OperationFailedError,
    SessionExpiredError,
    StateUnavailableError,
)
from .locks import FileLock
from .manifest import (
    REASON_VIDEO_NOT_REQUESTED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    Manifest,
    manifest_path,
)
from .models import MediaItem, Post
from .parsers import connection_cursor, connection_posts

log = logging.getLogger("instagram_sdk")

SOURCES = ("saved", "collection", "user_posts")
MODES = ("auto", "incremental", "full")

DEFAULT_KNOWN_STOP = 8  # > 3 pinned posts at the top of a profile
BATCH_PAUSE_S: tuple[float, float] = (25.0, 50.0)
HOURLY_RESERVE = 40
DAILY_RESERVE = 150
BUDGET_RECHECK_S = 300.0
FAILURE_PAUSE_S: tuple[float, float] = (60.0, 120.0)
MAX_CONSECUTIVE_FAILURES = 2
FALLBACK_PER_BATCH = 2
CANCEL_POLL_S = 5.0

RUNNING_STATES = ("running", "waiting")
FINAL_STATES = ("done", "partial", "cancelled", "error", "interrupted")

_COUNTER_KEYS = ("pages", "posts_seen", "posts_known", "posts_new", "downloaded",
                 "adopted", "skipped", "failed", "bytes", "fallback_resolves")


class _BudgetStop(Exception):
    """Internal: the account budget ran out; the job ends ``partial``."""

    def __init__(self, cause: BaseException | None) -> None:
        wait = getattr(cause, "retry_after_s", 0.0) or 0.0
        self.message = (f"budget: {cause or 'request budget reached'}; resume point kept, "
                        f"start again in ~{int(wait)} s")
        super().__init__(self.message)


class BulkJobBusyError(RuntimeError):
    """Another bulk job of the same account is already running."""


@dataclass(frozen=True)
class BulkSpec:
    source: str
    target: str | None = None
    download_videos: bool = False
    mode: str = "auto"
    known_stop: int = DEFAULT_KNOWN_STOP
    max_pages: int | None = None
    max_posts: int | None = None

    @property
    def source_key(self) -> str:
        if self.source == "saved":
            return "saved"
        if self.source == "collection":
            return f"collection:{self.target}"
        return f"user:{(self.target or '').casefold()}"

    def job_id(self, account: str) -> str:
        return f"{account.casefold()}:{self.source_key}"


def make_spec(source: str, target: str | None = None, *, download_videos: bool = False,
              mode: str = "auto", known_stop: int | None = None,
              max_pages: int | None = None, max_posts: int | None = None) -> BulkSpec:
    """Validate the tool arguments (no network)."""
    src = (source or "").strip().lower()
    tgt = target.strip() if isinstance(target, str) and target.strip() else None
    if src not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}; got {source!r}")
    if src == "collection":
        if tgt == saved.ALL_MEDIA_COLLECTION_ID:
            src, tgt = "saved", None
        elif not tgt or not tgt.isdigit():
            raise ValueError("source='collection' needs target = a numeric collection id "
                             "(from get_saved_collections)")
    elif src == "user_posts":
        if not tgt:
            raise ValueError("source='user_posts' needs target = a username")
        tgt = normalize_username(tgt)
    else:
        tgt = None
    m = (mode or "auto").strip().lower()
    if m not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}; got {mode!r}")
    k = DEFAULT_KNOWN_STOP if known_stop is None else int(known_stop)
    if k < 1:
        raise ValueError("known_stop must be >= 1")
    for name, value in (("max_pages", max_pages), ("max_posts", max_posts)):
        if value is not None and int(value) < 1:
            raise ValueError(f"{name} must be >= 1 (or omitted)")
    return BulkSpec(src, tgt, bool(download_videos), m, k,
                    int(max_pages) if max_pages else None,
                    int(max_posts) if max_posts else None)


def parse_job_id(job_id: str) -> tuple[str, str]:
    account, sep, source_key = (job_id or "").strip().partition(":")
    if not sep or not account or not source_key:
        raise ValueError(f"invalid job_id {job_id!r}: expected '<account>:<source>'")
    return account, source_key


def runner_lock(downloads_dir: Path, account: str) -> FileLock:
    return FileLock(downloads_dir.expanduser() / account.casefold() / ".bulk.lock")


def _is_running_elsewhere(downloads_dir: Path, account: str) -> bool:
    """True if some runner (this process or another) holds the account lock."""
    probe = runner_lock(downloads_dir, account)
    try:
        if probe.try_acquire():
            probe.release()
            return False
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# Feed pages (one per batch)
# --------------------------------------------------------------------------
async def fetch_source_page(ig: Any, spec: BulkSpec, upstream: str | None) -> PageResult[Post]:
    """One page of the source, starting at Instagram cursor ``upstream``."""
    start = CursorState(upstream, 0) if upstream else None
    if spec.source in ("saved", "collection"):
        return await saved.fetch_saved_page(
            ig, collection_id=spec.target if spec.source == "collection" else None,
            limit=None, start=start, max_pages=1,
        )
    handle = spec.target or ""

    async def fetch_page(up: str | None) -> tuple[list[Post], str | None]:
        if up is None:
            conn = await ig.graphql("profile_posts_first", username=handle)
        else:
            conn = await ig.graphql("profile_posts_page", username=handle, after=up)
        return connection_posts(conn), connection_cursor(conn)

    # Whole grid, reels included; no play-count lookups (they cost requests).
    return await paginate(fetch_page, limit=None, max_pages=1, start=start,
                          key_of=lambda p: p.id)


def _needs_video_fallback(post: Post) -> bool:
    return any(m.media_type == 2 and not m.video_url for m in post.media)


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------
class BulkJob:
    """Runs one bulk job of one account. ``claim()`` then ``await run()``."""

    def __init__(
        self,
        runtime: Any,
        account: str,
        spec: BulkSpec,
        *,
        downloads_dir: Path,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime = runtime
        self.account = account
        self.spec = spec
        self.downloads_dir = downloads_dir.expanduser()
        self.job_id = spec.job_id(account)
        self._sleep = sleep or asyncio.sleep
        self._uniform = uniform
        self._clock = clock
        self._lock = runner_lock(self.downloads_dir, account)
        self.manifest_path = manifest_path(self.downloads_dir, account)
        self.manifest: Manifest | None = None
        self.counters: dict[str, int] = {k: 0 for k in _COUNTER_KEYS}
        self._batches = 0
        self._failures = 0

    # -- lifecycle ------------------------------------------------------
    def claim(self) -> dict[str, Any]:
        """Take the account's runner lock and register the job (sync, no network)."""
        if not self._lock.try_acquire():
            running = [j["job_id"] for j in list_jobs(self.downloads_dir, [self.account])
                       if j["status"] in RUNNING_STATES]
            raise BulkJobBusyError(
                f"another bulk download of {self.account!r} is running "
                f"({', '.join(running) or 'in another process'}); "
                "wait for it or cancel it with cancel_bulk_download")
        try:
            self.manifest = Manifest(self.manifest_path, account=self.account, clock=self._clock)
            self.manifest.create_job(self.job_id, source=self.spec.source,
                                     target=self.spec.target, known_stop=self.spec.known_stop)
            now = self._clock()
            self.manifest.update_job(
                self.job_id, status="running", phase=None, error=None, wait_until=None,
                download_videos=int(self.spec.download_videos), mode=self.spec.mode,
                known_stop=self.spec.known_stop, max_pages=self.spec.max_pages,
                max_posts=self.spec.max_posts, counters=self.counters,
                owner=f"{socket.gethostname()}:{os.getpid()}", heartbeat=now,
                cancel_requested=0, started_at=now, finished_at=None,
            )
        except BaseException:
            self._release()
            raise
        return self.status()

    def _release(self) -> None:
        if self.manifest is not None:
            self.manifest.close()
            self.manifest = None
        self._lock.release()

    def status(self) -> dict[str, Any]:
        assert self.manifest is not None
        job = self.manifest.job(self.job_id) or {}
        return summarize(job, account=self.account, manifest=self.manifest, running=True)

    async def run(self) -> dict[str, Any]:
        """Run to the end (or a cap, a cancel, an error). Returns the final status."""
        if self.manifest is None:
            self.claim()
        assert self.manifest is not None
        final: dict[str, Any] = {}
        try:
            outcome = await self._run_phases()
            self._finish(outcome)
        except asyncio.CancelledError:
            self._finish("interrupted", error="the server stopped; start it again to resume")
            final = self.status()
            self._release()
            raise
        except _BudgetStop as stop:
            self._finish("partial", error=stop.message)
        except (AccountBlockedError, SessionExpiredError, AccountError,
                StateUnavailableError) as exc:
            self._finish("error", error=str(exc))
        except (OperationFailedError, ValueError) as exc:
            self._finish("error", error=str(exc))
        except Exception as exc:  # never leave a job "running" behind
            log.exception("instagram_sdk: bulk job %s crashed", self.job_id)
            self._finish("error", error=f"{type(exc).__name__}: {exc}")
        if not final:
            final = self.status()
            final["running"] = False
            self._release()
        return final

    def _finish(self, status: str, *, error: str | None = None) -> None:
        assert self.manifest is not None
        if status == "end":
            status = "done"
        self.manifest.update_job(self.job_id, status=status, error=error, wait_until=None,
                                 counters=self.counters, finished_at=self._clock(),
                                 heartbeat=self._clock())
        log.info("instagram_sdk: bulk job %s %s %s", self.job_id, status, self.counters)

    def _job(self) -> dict[str, Any]:
        assert self.manifest is not None
        job = self.manifest.job(self.job_id)
        assert job is not None
        return job

    def _cancelled(self) -> bool:
        assert self.manifest is not None
        return self.manifest.cancel_requested(self.job_id)

    async def _wait(self, seconds: float) -> bool:
        """Sleep in small steps; True if a cancel arrived meanwhile."""
        deadline = self._clock() + max(0.0, seconds)
        while True:
            if self._cancelled():
                return True
            left = deadline - self._clock()
            if left <= 0:
                return False
            await self._sleep(min(CANCEL_POLL_S, left))

    def _save(self, **fields: Any) -> None:
        assert self.manifest is not None
        self.manifest.update_job(self.job_id, counters=self.counters,
                                 heartbeat=self._clock(), **fields)

    # -- phases ---------------------------------------------------------
    def _plan(self) -> list[tuple[str, str | None]]:
        job = self._job()
        cursor = _cursor_upstream(job.get("cursor"))
        if self.spec.mode == "incremental":
            return [("head", None)]
        if self.spec.mode == "full":
            self._save(cursor=None, full_done=0)
            return [("full", None)]
        if job.get("full_done"):
            return [("head", None)]
        if cursor:
            return [("head", None), ("backfill", cursor)]
        return [("full", None)]

    async def _run_phases(self) -> str:
        outcome = "end"
        for phase, upstream in self._plan():
            outcome = await self._walk(phase, upstream)
            if outcome != "end":
                return outcome
        return outcome

    async def _budget_gate(self) -> bool:
        """Wait while the account budget is inside the reserve. True = cancelled."""
        rt = self.runtime
        settings = rt.settings
        per_hour, per_day = settings.max_requests_per_hour, settings.max_requests_per_day
        while True:
            acct = rt.resolve(self.account)
            budget = rt.state.budget(acct, per_hour=per_hour, per_day=per_day)
            reason = None
            if per_hour and budget.used("general", HOUR_S) >= per_hour - HOURLY_RESERVE:
                reason = "hourly budget (keeping a reserve for interactive use)"
            elif per_day and budget.used("general", DAY_S) >= per_day - DAILY_RESERVE:
                reason = "daily budget (keeping a reserve for interactive use)"
            if reason is None:
                if self._job().get("status") == "waiting":
                    self._save(status="running", wait_until=None, error=None)
                return False
            until = self._clock() + BUDGET_RECHECK_S
            self._save(status="waiting", wait_until=until, error=f"waiting: {reason}")
            if await self._wait(BUDGET_RECHECK_S):
                return True

    async def _walk(self, phase: str, upstream: str | None) -> str:
        """Walk pages from ``upstream``. Returns end | head_stop | partial | cancelled."""
        self._save(phase=phase)
        consecutive_known = 0
        first_page = True
        while True:
            if self._cancelled():
                return "cancelled"
            if self.spec.max_pages and self.counters["pages"] >= self.spec.max_pages:
                return "partial"
            if self._batches > 0 and await self._wait(self._uniform(*BATCH_PAUSE_S)):
                return "cancelled"
            if await self._budget_gate():
                return "cancelled"
            self._batches += 1
            try:
                page, extra = await self.runtime.run(
                    self.account, lambda ig: self._read_batch(ig, upstream))
            except BudgetExceededError as exc:
                # Nothing of this page was processed: the saved cursor (start
                # of this page, or the previous page's next) is the resume point.
                raise _BudgetStop(exc) from exc
            except (AccountBlockedError, SessionExpiredError, AccountError,
                    StateUnavailableError):
                raise
            except OperationFailedError as exc:
                if phase == "backfill" and first_page and upstream:
                    # The saved Instagram cursor expired: walk again from the
                    # top; known posts are skipped without downloading.
                    log.warning("instagram_sdk: bulk %s: resume cursor rejected (%s); "
                                "restarting the walk from the top", self.job_id, exc)
                    phase, upstream = "full", None
                    self._save(phase=phase, cursor=None)
                    continue
                self._failures += 1
                if self._failures >= MAX_CONSECUTIVE_FAILURES:
                    raise
                if await self._wait(self._uniform(*FAILURE_PAUSE_S)):
                    return "cancelled"
                continue
            self._failures = 0
            first_page = False
            self.counters["pages"] += 1
            self.counters["fallback_resolves"] += extra

            for post in page.items:
                if self._cancelled():
                    if phase != "head":
                        self._save(cursor=_cursor_json(upstream))
                    return "cancelled"
                if self.spec.max_posts and self.counters["posts_new"] >= self.spec.max_posts:
                    if phase != "head":
                        self._save(cursor=_cursor_json(upstream))
                    return "partial"
                known = await self._process_post(post)
                if known:
                    consecutive_known += 1
                    if phase == "head" and consecutive_known >= self.spec.known_stop:
                        self._save()
                        return "end"
                else:
                    consecutive_known = 0
                self._save()

            nxt = page.next.upstream if page.next else None
            if page.stopped == STOP_BUDGET:
                # Cut by the budget after some items: resume at ``page.next``.
                if phase != "head":
                    self._save(cursor=_cursor_json(nxt))
                raise _BudgetStop(page.error)
            if phase != "head":
                self._save(cursor=_cursor_json(nxt))
            if nxt is None:
                if phase != "head":
                    self._save(full_done=1, cursor=None)
                return "end"
            upstream = nxt

    async def _read_batch(self, ig: Any, upstream: str | None) -> tuple[PageResult[Post], int]:
        """Under the navigation lock: one feed page (+ rare video fallbacks)."""
        page = await fetch_source_page(ig, self.spec, upstream)
        resolved = 0
        if self.spec.download_videos:
            items = list(page.items)
            for i, post in enumerate(items):
                if resolved >= FALLBACK_PER_BATCH:
                    break
                if not _needs_video_fallback(post) or self._known(post):
                    continue
                try:
                    items[i] = await media_ep.resolve_post_for_download(
                        ig, id_or_url=post.shortcode)
                    resolved += 1
                except ValueError:
                    continue
                except BudgetExceededError:
                    # Keep the page already read; the video is retried later.
                    break
            page.items = items
        return page, resolved

    def _known(self, post: Post) -> bool:
        assert self.manifest is not None
        return self.manifest.is_known(post.pk, len(post.media),
                                      download_videos=self.spec.download_videos)

    # -- media ------------------------------------------------------------
    async def _process_post(self, post: Post) -> bool:
        """Download what is missing of one post. True if it was already known."""
        assert self.manifest is not None
        m = self.manifest
        author = post.author.username if post.author else None
        self.counters["posts_seen"] += 1
        m.note_post(pk=post.pk, shortcode=post.shortcode, author=author,
                    taken_at=post.taken_at, media_count=len(post.media),
                    source=self.spec.source_key)
        if self._known(post):
            self.counters["posts_known"] += 1
            return True
        self.counters["posts_new"] += 1
        for idx, item in enumerate(post.media):
            row = m.leaf(post.pk, idx)
            if m.leaf_is_known(row, download_videos=self.spec.download_videos):
                continue
            await self._process_leaf(post, idx, item, author)
        return False

    async def _process_leaf(self, post: Post, idx: int, item: MediaItem,
                            author: str | None) -> None:
        assert self.manifest is not None
        m = self.manifest
        is_video = item.media_type == 2
        kind = "video" if is_video else "image"
        base = dict(pk=post.pk, idx=idx, shortcode=post.shortcode, kind=kind,
                    author=author, taken_at=post.taken_at)
        if is_video and not self.spec.download_videos:
            m.record(status=STATUS_SKIPPED, reason=REASON_VIDEO_NOT_REQUESTED, **base)
            self.counters["skipped"] += 1
            return
        url = item.video_url if is_video else item.image_url
        if not url or not downloader.is_downloadable_media_url(url):
            m.record(status=STATUS_FAILED, reason="no_url" if not url else "not_cdn", **base)
            self.counters["failed"] += 1
            return

        def path_for(ext: str) -> Path:
            return downloader.media_path(self.downloads_dir, self.account, post.shortcode,
                                         post.pk, idx, ext)

        probe = path_for("jpg")
        existing = downloader.existing_media_file(probe.parent, probe.stem)
        if existing is not None:
            # Saved earlier (by download_media or a run that died before
            # writing the manifest): adopt it instead of fetching again.
            m.record(status=STATUS_DONE, saved_path=str(existing),
                     byte_size=existing.stat().st_size,
                     sha256=downloader.sha256_of(existing), **base)
            self.counters["adopted"] += 1
            return
        max_bytes = None if is_video else downloader.IMAGE_MAX_BYTES
        async with self.runtime.download_semaphore():
            out = await downloader.stream_to_file(
                self.runtime.http_client(), url, path_for_ext=path_for, max_bytes=max_bytes)
        if out.ok and out.path is not None:
            m.record(status=STATUS_DONE, saved_path=str(out.path), byte_size=out.byte_size,
                     sha256=out.sha256, content_type=out.content_type, **base)
            self.counters["downloaded"] += 1
            self.counters["bytes"] += out.byte_size
        elif out.reason == downloader.REASON_TOO_LARGE:
            m.record(status=STATUS_SKIPPED, reason=out.reason, **base)
            self.counters["skipped"] += 1
        else:
            m.record(status=STATUS_FAILED, reason=out.reason, **base)
            self.counters["failed"] += 1


def _cursor_json(upstream: str | None) -> str | None:
    return json.dumps({"u": upstream}) if upstream else None


def _cursor_upstream(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = json.loads(raw).get("u")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------
# Status (no network; works from any process)
# --------------------------------------------------------------------------
def summarize(job: dict[str, Any], *, account: str, manifest: Manifest,
              running: bool | None = None) -> dict[str, Any]:
    status = job.get("status")
    if running is None:
        running = status in RUNNING_STATES and _is_running_elsewhere(
            manifest.path.parent.parent, account)
        if status in RUNNING_STATES and not running:
            status = "interrupted"
    source_key = (job.get("job_id") or ":").split(":", 1)[1]
    return {
        "job_id": job.get("job_id"),
        "account": account,
        "source": job.get("source"),
        "target": job.get("target"),
        "status": status,
        "running": bool(running) and status in RUNNING_STATES,
        "phase": job.get("phase"),
        "mode": job.get("mode"),
        "download_videos": job.get("download_videos"),
        "full_done": job.get("full_done"),
        "resumable": bool(job.get("cursor")) and not job.get("full_done"),
        "cancel_requested": job.get("cancel_requested"),
        "this_run": job.get("counters") or {},
        "manifest_totals": manifest.totals(source=source_key),
        "error": job.get("error"),
        "wait_until": job.get("wait_until"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "manifest_path": str(manifest.path),
        "media_dir": str(manifest.path.parent),
    }


def job_status(downloads_dir: Path, job_id: str) -> dict[str, Any] | None:
    account, _ = parse_job_id(job_id)
    m = Manifest.open_existing(manifest_path(downloads_dir, account), account=account)
    if m is None:
        return None
    with m:
        job = m.job(job_id)
        return summarize(job, account=account, manifest=m) if job else None


def list_jobs(downloads_dir: Path, accounts: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for account in accounts:
        m = Manifest.open_existing(manifest_path(downloads_dir, account), account=account)
        if m is None:
            continue
        with m:
            out.extend(summarize(j, account=account, manifest=m) for j in m.jobs())
    return out


def request_cancel(downloads_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Ask a job to stop (it does within seconds, from any process).

    A job that is not running is just marked ``cancelled``; its resume cursor
    is kept, so starting it again continues where it was.
    """
    account, _ = parse_job_id(job_id)
    m = Manifest.open_existing(manifest_path(downloads_dir, account), account=account)
    if m is None:
        return None
    with m:
        job = m.job(job_id)
        if job is None:
            return None
        m.request_cancel(job_id)
        if not _is_running_elsewhere(downloads_dir, account):
            if job.get("status") in RUNNING_STATES or job.get("status") == "new":
                m.update_job(job_id, status="cancelled", finished_at=time.time())
        job = m.job(job_id)
        return summarize(job, account=account, manifest=m) if job else None
