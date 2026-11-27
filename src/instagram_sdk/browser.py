"""One Chromium process, one ``BrowserContext`` per account, one fixed tab each.

Why keep a warm browser: launching Chromium takes seconds, and every request
is an in-page ``fetch`` issued from an instagram.com tab, so a document must be
loaded. The tab is reused across calls (its HTML carries the GraphQL tokens)
and every subresource that is not the document or our own fetches is aborted
(images, media, fonts, CSS and, by default, scripts) — that is where the RAM
went in the old navigate-and-scroll design.

Lifecycle fixes over the previous ``BrowserSession``:

* a failed ``chromium.launch()`` stops the Playwright driver it had started;
* a dead browser (``disconnected``), a crashed or closed tab, or a closed
  context drops the cached state, so the next call relaunches instead of
  failing until the process restarts;
* an operation interrupted by a closed target surfaces as
  ``BrowserUnavailableError(retryable=True)`` and the runtime retries it once.

``BrowserSession`` at the bottom is the library facade ``social-ingest`` uses.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Union

from .accounts import Account, session_fingerprint
from .auth import VIEWPORT, browser_context_options, user_agent_override
from .errors import BrowserUnavailableError, SessionExpiredError
from .pacer import Pacer
from .settings import DEFAULT_IDLE_TIMEOUT_S, Settings
from .tokens import Tokens

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

    from .inpage import InPageClient

log = logging.getLogger("instagram_sdk")

# Stripped on every page so instagram.com's scripts can't trivially see that
# the browser is automated. Minimal hardening, not a stealth suite.
_WEBDRIVER_SCRUB = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

BLOCKED_RESOURCES = frozenset({"image", "media", "font", "stylesheet", "script"})


def blocked_resource_types(block_js: bool) -> frozenset[str]:
    return BLOCKED_RESOURCES if block_js else BLOCKED_RESOURCES - {"script"}


def _install_hint(exc: BaseException) -> str:
    hint = (
        f"Failed to launch Chromium: {exc}\n"
        "Install the browser binaries: python -m playwright install chromium"
    )
    if platform.system() == "Linux":
        hint += (
            "\nIf native libraries are missing on Linux/WSL2: "
            "sudo python -m playwright install-deps chromium"
        )
    return hint


async def _quiet(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:
        pass


def is_target_closed(exc: BaseException) -> bool:
    """True for Playwright errors caused by a closed/crashed page or browser."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "target closed",
            "target page, context or browser has been closed",
            "has been closed",
            "browser has disconnected",
            "page crashed",
            "connection closed",
        )
    )


@dataclass
class AccountContext:
    handle: str
    context: "BrowserContext"
    page: "Page"
    session_fp: list[Any] | None
    pacer: Pacer
    tokens: Tokens | None = None
    last_used: float = field(default_factory=time.monotonic)
    # Public, short-lived data (e.g. reel play counts per author, 10 min).
    cache: dict[str, Any] = field(default_factory=dict)
    # Last document loaded in the fixed tab: (path, html, monotonic time).
    last_html: tuple[str, str, float] | None = None
    dead: bool = False
    cdp: Any = None

    def alive(self) -> bool:
        if self.dead:
            return False
        try:
            return not self.page.is_closed()
        except Exception:
            return False


class BrowserManager:
    def __init__(
        self,
        settings: Settings,
        *,
        lock: asyncio.Lock | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        max_requests_per_call: int | None = 20,
    ) -> None:
        self._settings = settings
        self._lock = lock
        self._pw_factory = playwright_factory
        self._max_requests_per_call = max_requests_per_call
        self._pw: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._contexts: dict[str, AccountContext] = {}
        self._idle_tasks: dict[str, asyncio.Task] = {}
        self._user_agent: str | None = None
        self._browser_version: str | None = None

    # ------------------------------------------------------------------
    @property
    def user_agent(self) -> str | None:
        return self._user_agent

    @property
    def idle_timeout_s(self) -> int:
        return self._settings.idle_timeout_s

    def contexts(self) -> dict[str, AccountContext]:
        return dict(self._contexts)

    def _factory(self) -> Any:
        if self._pw_factory is not None:
            return self._pw_factory()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(
                "Playwright is not installed. Run: pip install playwright && "
                "python -m playwright install chromium",
                retryable=False,
            ) from exc
        return async_playwright()

    async def _ensure_browser(self) -> "Browser":
        browser = self._browser
        if browser is not None:
            try:
                if browser.is_connected():
                    return browser
            except Exception:
                pass
            self._on_disconnected()
        pw = await self._factory().start()
        try:
            kwargs: dict[str, Any] = {"headless": self._settings.headless}
            if self._settings.channel:
                kwargs["channel"] = self._settings.channel
            browser = await pw.chromium.launch(**kwargs)
        except Exception as exc:
            await _quiet(pw.stop())
            raise BrowserUnavailableError(_install_hint(exc), retryable=False) from exc
        self._pw = pw
        self._browser = browser
        self._browser_version = browser.version
        try:
            browser.on("disconnected", lambda *_: self._on_disconnected(browser))
        except Exception:
            pass
        return browser

    def _on_disconnected(self, browser: Any = None) -> None:
        """Synchronous: forget everything bound to a dead browser."""
        if browser is not None and browser is not self._browser:
            return
        log.warning("instagram_sdk: Chromium disconnected; will relaunch on next call")
        for task in self._idle_tasks.values():
            task.cancel()
        self._idle_tasks.clear()
        for actx in self._contexts.values():
            actx.dead = True
        self._contexts.clear()
        self._browser = None
        pw, self._pw = self._pw, None
        if pw is not None:
            try:
                asyncio.get_running_loop().create_task(_quiet(pw.stop()))
            except RuntimeError:
                pass

    def _drop(self, handle: str, actx: AccountContext) -> None:
        actx.dead = True
        if self._contexts.get(handle) is actx:
            del self._contexts[handle]

    # ------------------------------------------------------------------
    async def context_for(self, account: Account) -> AccountContext:
        key = account.key
        fp = session_fingerprint(account.session_path)
        if fp is None:
            raise SessionExpiredError(
                f"Instagram session file not found at {account.session_path}. "
                "Run: python -m scripts.auth_login"
            )
        actx = self._contexts.get(key)
        if actx is not None:
            browser_ok = False
            try:
                browser_ok = self._browser is not None and self._browser.is_connected()
            except Exception:
                browser_ok = False
            if browser_ok and actx.alive() and actx.session_fp == fp:
                self._cancel_idle(key)
                return actx
            # Session file changed (re-login) or the tab died: rebuild.
            await self._close_context(key)

        browser = await self._ensure_browser()
        # Per-account fingerprint: the sidecar written at login, else the env.
        fp_opts = account.fingerprint_options(
            locale=self._settings.locale,
            timezone_id=self._settings.timezone_id,
            viewport=VIEWPORT,
        )
        options = browser_context_options(browser.version, **fp_opts)
        self._user_agent = str(options["user_agent"])
        context = None
        try:
            context = await browser.new_context(
                storage_state=str(account.session_path),
                service_workers="block",
                **options,
            )
            await context.add_init_script(_WEBDRIVER_SCRUB)
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            await cdp.send(
                "Emulation.setUserAgentOverride",
                user_agent_override(browser.version, str(fp_opts["locale"])),
            )
            blocked = blocked_resource_types(self._settings.block_js)

            async def _route(route: Any) -> None:
                try:
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.fallback()
                except Exception:
                    pass

            await page.route("**/*", _route)
        except BrowserUnavailableError:
            raise
        except Exception as exc:
            if context is not None:
                await _quiet(context.close())
            if not self._contexts:
                await self._shutdown_browser()
            if is_target_closed(exc):
                raise BrowserUnavailableError(str(exc), retryable=True) from exc
            raise
        actx = AccountContext(
            handle=account.handle,
            context=context,
            page=page,
            session_fp=fp,
            pacer=Pacer(max_requests_per_call=self._max_requests_per_call),
            cdp=cdp,
        )
        for event, target in (("crash", page), ("close", page), ("close", context)):
            try:
                target.on(event, lambda *_a, h=key, a=actx: self._drop(h, a))
            except Exception:
                pass
        self._contexts[key] = actx
        return actx

    # ------------------------------------------------------------------
    def schedule_idle(self, handle: str) -> None:
        key = handle.casefold()
        self._cancel_idle(key)
        if self._settings.idle_timeout_s <= 0 or key not in self._contexts:
            return
        loop = asyncio.get_running_loop()
        self._idle_tasks[key] = loop.create_task(self._idle_close_after(key))

    def _cancel_idle(self, key: str) -> None:
        task = self._idle_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _idle_close_after(self, key: str) -> None:
        timeout = self._settings.idle_timeout_s
        try:
            await asyncio.sleep(timeout)
            if self._lock is not None:
                async with self._lock:
                    await self._idle_close_locked(key, timeout)
            else:
                await self._idle_close_locked(key, timeout)
        except asyncio.CancelledError:
            return

    async def _idle_close_locked(self, key: str, timeout: float) -> None:
        actx = self._contexts.get(key)
        if actx is None:
            return
        # A call may have come in while we waited for the lock.
        if time.monotonic() - actx.last_used < timeout - 0.5:
            return
        self._idle_tasks.pop(key, None)
        await self._close_context(key)
        if not self._contexts:
            await self._shutdown_browser()

    async def _close_context(self, key: str) -> None:
        self._cancel_idle(key)
        actx = self._contexts.pop(key, None)
        if actx is None:
            return
        actx.dead = True
        await _quiet(actx.context.close())

    async def close_account(self, handle: str) -> None:
        await self._close_context(handle.casefold())
        if not self._contexts:
            await self._shutdown_browser()

    async def _shutdown_browser(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            await _quiet(browser.close())
        pw, self._pw = self._pw, None
        if pw is not None:
            await _quiet(pw.stop())

    async def shutdown(self) -> None:
        """Close everything. Idempotent."""
        for key in list(self._idle_tasks):
            self._cancel_idle(key)
        for key in list(self._contexts):
            await self._close_context(key)
        await self._shutdown_browser()


# --------------------------------------------------------------------------
# Library facade (social-ingest)
# --------------------------------------------------------------------------
class BrowserSession:
    """Backwards-compatible entry point for library users.

    ``page()`` now yields an ``InPageClient`` instead of a Playwright ``Page``;
    the ``endpoints.*.fetch_*`` functions take it as their first argument, so
    callers that only pass it through do not change. Library calls have no
    per-call request cap (the caller owns its loop); pauses still apply.
    """

    def __init__(
        self,
        session_path: Union[Path, Callable[[], Path]],
        *,
        idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
        headless: bool = True,
    ) -> None:
        from dataclasses import replace

        from .accounts import AccountRegistry
        from .runtime import Runtime

        source = session_path
        settings = replace(
            Settings.from_env(), idle_timeout_s=idle_timeout_s, headless=headless
        )
        fixed = source if callable(source) else (lambda: Path(source))
        self._runtime = Runtime(
            settings,
            registry=AccountRegistry(settings, fixed=fixed),
            max_requests_per_call=None,
        )

    @property
    def runtime(self) -> Any:
        return self._runtime

    @property
    def user_agent(self) -> str | None:
        return self._runtime.browser.user_agent

    @asynccontextmanager
    async def page(self) -> AsyncIterator["InPageClient"]:
        async with self._runtime.session(None) as client:
            yield client

    async def shutdown(self) -> None:
        await self._runtime.shutdown()
