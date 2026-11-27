"""Process-wide runtime: browser, lock, account state and the download client.

``Runtime.run(account, fn)`` is the one door every tool goes through:

1. resolve the account (this PR: the single session file the server finds);
2. if its stored state is ``blocked``, a live ``cooldown`` or ``expired`` for
   the same session file → raise at once, without touching the network;
3. take the lock (one Instagram request stream per process, never two);
4. get the account's warm context and build an ``InPageClient``;
5. run ``fn(client)``; afterwards schedule the idle close;
6. a ``BrowserUnavailableError(retryable=True)`` (Chromium died) repeats
   steps 4-5 once — it is a local failure and the requests are reads.

Allowed state here is process-level only. Nothing per client or per
conversation is kept.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from .accounts import Account, AccountStateStore
from .browser import BrowserManager
from .endpoints.users import UserIdCache
from .errors import OperationFailedError, SessionExpiredError, UnknownAccountError
from .guard import BlockSignal
from .inpage import InPageClient
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
        session_source: Callable[[], Path] | None = None,
        browser: BrowserManager | None = None,
        max_requests_per_call: int | None = MAX_REQUESTS_PER_CALL,
    ) -> None:
        self.settings = settings
        self._session_source = session_source or settings.resolve_session_path
        self._lock = asyncio.Lock()
        self.browser = browser or BrowserManager(
            settings, lock=self._lock, max_requests_per_call=max_requests_per_call
        )
        self.state = AccountStateStore(settings.state_dir)
        self.user_ids = UserIdCache(settings.state_dir / "user_ids.json")
        self._http_client: Any = None
        self._download_sem: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------
    def current_account(self) -> Account:
        return Account.from_session(self._session_source())

    def resolve(self, account: str | None) -> Account:
        acct = self.current_account()
        if account is not None:
            wanted = account.strip().lstrip("@").casefold()
            if wanted != acct.key:
                raise UnknownAccountError(
                    f"Unknown account {account!r}. Available: {acct.handle!r}."
                )
        return acct

    async def _acquire(self) -> None:
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=LOCK_WAIT_S)
        except asyncio.TimeoutError:
            raise OperationFailedError(
                "lock", f"another Instagram call is still running after {LOCK_WAIT_S}s"
            ) from None

    @asynccontextmanager
    async def session(self, account: str | None) -> AsyncIterator[InPageClient]:
        """Hold the lock and yield a client for one logical call."""
        acct = self.resolve(account)
        if not acct.session_path.exists():
            raise SessionExpiredError(
                f"Instagram session file not found at {acct.session_path}. "
                "Run: python -m scripts.auth_login"
            )
        self.state.check(acct)
        await self._acquire()
        try:
            # Again under the lock: a call queued behind one that just got
            # blocked must not hit Instagram (nor add a strike).
            self.state.check(acct)
            actx = await self.browser.context_for(acct)

            async def on_block(signal: BlockSignal) -> Exception:
                try:
                    # Never raises: if the disk write fails the block stays
                    # in memory for this process.
                    return self.state.record(acct, signal)
                finally:
                    await self.browser.close_account(acct.handle)

            client = InPageClient(actx, on_block=on_block, user_ids=self.user_ids)
            client.new_call()
            try:
                yield client
            finally:
                actx.last_used = time.monotonic()
                if not actx.dead:
                    self.browser.schedule_idle(acct.handle)
        finally:
            self._lock.release()

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
        _runtime = Runtime(Settings.from_env())
    return _runtime


def reset_runtime(runtime: Runtime | None = None) -> None:
    """Replace the singleton (tests)."""
    global _runtime
    _runtime = runtime
