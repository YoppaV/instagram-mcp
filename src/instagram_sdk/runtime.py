"""Process-wide runtime: accounts, browser, locks, state and the download client.

``Runtime.run(account, fn)`` is the one door every tool goes through:

1. resolve the account (``AccountRegistry``: explicit ``account`` or the
   default; validated against the session files on disk);
2. if its stored state is ``blocked``, a live ``cooldown`` (its own or the
   global pause after another account was stopped) or ``expired`` for the
   same session file → raise at once, without touching the network;
3. take the global lock: the process ``asyncio.Lock`` and then the file lock
   ``<state>/nav.lock`` shared by every server process (180 s at most);
4. if the last account that drove Instagram was another one, pause 5-10 s;
5. get the account's warm context (one ``BrowserContext`` per account, one
   Chromium) and build an ``InPageClient`` with the account's persisted
   hourly/daily budget;
6. run ``fn(client)``; afterwards persist budget and last-request time,
   count failures for the failure brake, schedule the idle close and release
   the locks in reverse order;
7. a ``BrowserUnavailableError(retryable=True)`` (Chromium died) repeats
   steps 5-6 once — it is a local failure and the requests are reads.

Allowed state here is process-level only. Nothing per client or per
conversation is kept (no "current account", no cursors in memory).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from .accounts import Account, AccountRegistry, AccountStateStore
from .browser import BrowserManager
from .endpoints.users import UserIdCache
from .errors import (
    OperationFailedError,
    SessionExpiredError,
)
from .guard import BlockSignal
from .inpage import InPageClient
from .locks import FileLock
from .pacer import MAX_REQUESTS_PER_CALL
from .settings import Settings

log = logging.getLogger("instagram_sdk")

T = TypeVar("T")

LOCK_WAIT_S = 180
DOWNLOAD_CONCURRENCY = 2


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: AccountRegistry | None = None,
        session_source: Callable[[], Path] | None = None,
        browser: BrowserManager | None = None,
        max_requests_per_call: int | None = MAX_REQUESTS_PER_CALL,
    ) -> None:
        self.settings = settings
        if registry is None:
            # ``session_source`` = one fixed session file (tests, old callers).
            registry = AccountRegistry(settings, fixed=session_source)
        self.registry = registry
        self._lock = asyncio.Lock()
        self._file_lock = FileLock(settings.state_dir / "nav.lock")
        self._file_lock_warned = False
        self.browser = browser or BrowserManager(
            settings, lock=self._lock, max_requests_per_call=max_requests_per_call
        )
        self.state = AccountStateStore(settings.state_dir)
        self.user_ids = UserIdCache(settings.state_dir / "user_ids.json")
        self._http_client: Any = None
        self._download_sem: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------
    def resolve(self, account: str | None) -> Account:
        return self.registry.resolve(account)

    async def _acquire(self) -> None:
        deadline = time.monotonic() + LOCK_WAIT_S
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=LOCK_WAIT_S)
        except asyncio.TimeoutError:
            raise OperationFailedError(
                "lock", f"another Instagram call is still running after {LOCK_WAIT_S}s"
            ) from None
        try:
            got = await self._file_lock.acquire(max(0.0, deadline - time.monotonic()))
        except OSError as exc:
            # State dir unusable (read-only volume...): the process lock still
            # serializes this server; say so once and carry on.
            if not self._file_lock_warned:
                log.error("instagram_sdk: cross-process lock unavailable (%s); "
                          "only this process is serialized", exc)
                self._file_lock_warned = True
            return
        except BaseException:
            self._lock.release()
            raise
        if not got:
            self._lock.release()
            raise OperationFailedError(
                "lock",
                f"another instagram-mcp process is still using the browser after "
                f"{LOCK_WAIT_S}s ({self._file_lock.path})",
            )

    def _release(self) -> None:
        try:
            self._file_lock.release()
        finally:
            self._lock.release()

    @asynccontextmanager
    async def session(self, account: str | None) -> AsyncIterator[InPageClient]:
        """Hold the global lock and yield a client for one logical call."""
        acct = self.resolve(account)
        if not acct.session_path.exists():
            raise SessionExpiredError(
                f"Instagram session file not found at {acct.session_path}. "
                f"Run: python -m scripts.auth_login --username {acct.handle}"
            )
        self.state.check(acct)
        await self._acquire()
        try:
            # Again under the lock: a call queued behind one that just got
            # blocked (here or in another process) must not hit Instagram.
            self.state.check(acct)
            actx = await self.browser.context_for(acct)
            last = self.state.load_global().get("last_account")
            if last and last != acct.key:
                await actx.pacer.switch_pause()
            budget = self.state.budget(
                acct,
                per_hour=self.settings.max_requests_per_hour,
                per_day=self.settings.max_requests_per_day,
            )

            async def on_block(signal: BlockSignal) -> Exception:
                try:
                    # Never raises: if the disk write fails the block stays
                    # in memory for this process.
                    return self.state.record(acct, signal)
                finally:
                    await self.browser.close_account(acct.handle)

            client = InPageClient(actx, on_block=on_block, user_ids=self.user_ids)
            client.new_call()
            actx.pacer.budget = budget
            failed: OperationFailedError | None = None
            try:
                yield client
            except OperationFailedError as exc:
                failed = exc
                raise
            finally:
                pacer = actx.pacer
                pacer.budget = None
                made = pacer.requests_this_call > 0
                try:
                    self.state.save_usage(acct, budget if made else None,
                                          pacer.last_request_wall if made else None)
                    if failed is not None:
                        self.state.record_failure(acct, str(failed))
                    elif made:
                        self.state.record_success(acct)
                except Exception:  # state is best effort; never mask the result
                    log.exception("instagram_sdk: could not persist usage for %s", acct.handle)
                actx.last_used = time.monotonic()
                if not actx.dead:
                    self.browser.schedule_idle(acct.handle)
        finally:
            self._release()

    async def run(self, account: str | None, fn: Callable[[InPageClient], Awaitable[T]]) -> T:
        from .errors import BrowserUnavailableError

        for attempt in range(2):
            try:
                async with self.session(account) as client:
                    return await fn(client)
            except BrowserUnavailableError as exc:
                if not exc.retryable or attempt == 1:
                    raise
                log.warning("instagram_sdk: browser went away (%s); retrying once", exc)
                try:
                    await self.browser.close_account(self.resolve(account).handle)
                except Exception:
                    pass
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    def http_client(self) -> Any:
        """Shared httpx client for CDN downloads, with the browser's UA."""
        if self._http_client is None:
            import httpx

            ua = self.browser.user_agent
            self._http_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0),
                headers={"User-Agent": ua} if ua else None,
            )
        return self._http_client

    def download_semaphore(self) -> asyncio.Semaphore:
        if self._download_sem is None:
            self._download_sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        return self._download_sem

    async def shutdown(self) -> None:
        await self.browser.shutdown()
        client, self._http_client = self._http_client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Lazy per-process singleton, created on first use inside the event loop."""
    global _runtime
    if _runtime is None:
        settings = Settings.from_env()
        _runtime = Runtime(settings, registry=AccountRegistry(settings))
    return _runtime


def reset_runtime(runtime: Runtime | None = None) -> None:
    """Replace the singleton (tests)."""
    global _runtime
    _runtime = runtime
