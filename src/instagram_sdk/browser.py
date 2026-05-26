"""Lazy Playwright singleton with idle-timeout shutdown.

Why a singleton: launching Chromium takes 3–8s. If every MCP tool call started
a fresh browser, conversational use would be unbearable. We keep one warm
context per server process and close it after ``idle_timeout_s`` of inactivity
to free RAM.

Concurrency: tool calls are serialized behind an asyncio.Lock. Instagram is
aggressive about automation detection — a single, non-parallel request stream
through one context looks far more like a human than two tabs scraping at once.
Our scroll loops also install response handlers that would conflict if two
endpoints scraped concurrently.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from .auth import (
    LOCALE,
    TIMEZONE_ID,
    USER_AGENT,
    VIEWPORT,
    SessionExpiredError,
    is_login_redirect,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright


DEFAULT_IDLE_TIMEOUT_S = 180

# Stripped on every page so instagram.com's scripts can't trivially see that
# the browser is automated. Minimal, defensible hardening — not a full
# stealth suite.
_WEBDRIVER_SCRUB = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

_RELOGIN_HINT = "re-run: python -m scripts.auth_login"


class BrowserSession:
    """Single Playwright BrowserContext, lazily launched."""

    def __init__(
        self,
        session_path: Path,
        *,
        idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
        headless: bool = True,
    ) -> None:
        self._session_path = session_path
        self._idle_timeout_s = idle_timeout_s
        self._headless = headless

        self._lock = asyncio.Lock()
        self._pw: "Playwright | None" = None
        self._browser = None
        self._context: "BrowserContext | None" = None
        self._idle_task: asyncio.Task | None = None

    @property
    def session_path(self) -> Path:
        return self._session_path

    async def _ensure_started(self) -> "BrowserContext":
        if self._context is not None:
            return self._context

        if not self._session_path.exists():
            raise SessionExpiredError(
                f"Instagram session file not found at {self._session_path}. "
                f"Run: python -m scripts.auth_login"
            )

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && "
                "python -m playwright install chromium"
            ) from exc

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context(
            storage_state=str(self._session_path),
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale=LOCALE,
            timezone_id=TIMEZONE_ID,
        )
        await self._context.add_init_script(_WEBDRIVER_SCRUB)
        return self._context

    @asynccontextmanager
    async def page(self) -> AsyncIterator["Page"]:
        """Acquire an exclusive Page. Serializes tool calls.

        New page per call so each endpoint owns its response handlers and
        scroll position. The context is reused so cookies stay warm.
        """
        async with self._lock:
            context = await self._ensure_started()
            page = await context.new_page()
            self._cancel_idle_timer()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                self._schedule_idle_shutdown()

    async def assert_authenticated(self, page: "Page") -> None:
        """Raise SessionExpiredError if Instagram redirected us to login."""
        if is_login_redirect(page.url):
            raise SessionExpiredError(
                f"Instagram session expired — {_RELOGIN_HINT}"
            )

    def _schedule_idle_shutdown(self) -> None:
        self._cancel_idle_timer()
        if self._idle_timeout_s <= 0:
            return
        loop = asyncio.get_running_loop()
        self._idle_task = loop.create_task(self._idle_shutdown_after())

    def _cancel_idle_timer(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_shutdown_after(self) -> None:
        try:
            await asyncio.sleep(self._idle_timeout_s)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._shutdown_locked()

    async def shutdown(self) -> None:
        self._cancel_idle_timer()
        async with self._lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
