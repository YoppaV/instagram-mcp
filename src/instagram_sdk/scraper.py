"""Generic JSON-intercept + scroll loop reused by every list endpoint.

The shape: navigate to an instagram.com URL, install a
``page.on("response", ...)`` handler that filters by ``/api/v1/...`` path
fragment and feeds matching payloads to a parser, then scroll until the limit
is reached or the feed goes quiet. Instagram fires its own signed XHRs as you
scroll — we never craft requests ourselves.

Single-fetch endpoints (a single post, a profile header) use
``intercept_single_response`` instead — same handler pattern, no scrolling.

Pacing is deliberately slow. Instagram detects automation far more
aggressively than other networks, so every delay range here is conservative
and randomized; do not tighten them.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from .auth import SessionExpiredError, is_login_redirect

if TYPE_CHECKING:
    from playwright.async_api import Page, Response

# Conservative, randomized pacing — see module docstring.
PRE_NAV_DELAY_RANGE_S: tuple[float, float] = (1.5, 3.0)
SCROLL_DELAY_RANGE_S: tuple[float, float] = (3.0, 6.5)
SCROLL_PIXELS_RANGE: tuple[int, int] = (600, 900)
DEFAULT_MAX_SCROLLS = 60
EMPTY_SCROLLS_BEFORE_STOP = 3
SINGLE_FETCH_TIMEOUT_S = 25.0

_RELOGIN_HINT = "re-run: python -m scripts.auth_login"
_SESSION_EXPIRED_MSG = f"Instagram session expired — {_RELOGIN_HINT}"

T = TypeVar("T")
Extractor = Callable[[dict[str, Any]], list[T]]
# A fragment is either one path substring or several to match against.
Fragment = "str | tuple[str, ...]"


def _default_key_of(item: Any) -> str:
    """Default key extractor: use ``id`` (stable across Post/Reel shapes)."""
    return getattr(item, "id", "") or ""


def _url_matches(url: str, fragment: "str | tuple[str, ...]") -> bool:
    if isinstance(fragment, str):
        return fragment in url
    return any(frag in url for frag in fragment)


def _make_response_handler(
    fragment: "str | tuple[str, ...]",
    extractor: Extractor[T],
    collected: dict[str, T],
    order: list[str],
    key_of: Callable[[T], str],
) -> Callable[["Response"], Awaitable[None]]:
    async def on_response(response: "Response") -> None:
        if not _url_matches(response.url, fragment):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        for item in extractor(payload):
            key = key_of(item)
            if not key or key in collected:
                continue
            collected[key] = item
            order.append(key)

    return on_response


async def _sleep_jitter(rng: tuple[float, float]) -> None:
    await asyncio.sleep(random.uniform(*rng))


async def scroll_collect(
    page: "Page",
    initial_url: str,
    fragment: "str | tuple[str, ...]",
    extractor: Extractor[T],
    *,
    limit: int | None = None,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    empty_streak_stop: int = EMPTY_SCROLLS_BEFORE_STOP,
    key_of: Callable[[T], str] | None = None,
) -> list[T]:
    """Scroll ``initial_url`` and collect items parsed from ``fragment``.

    Stops when ``limit`` is reached, when scrolling produces no new items for
    ``empty_streak_stop`` iterations, or when ``max_scrolls`` is exhausted.
    ``fragment`` may be one path substring or a tuple of them (to merge two
    endpoints — e.g. a profile's embedded posts plus its paginated feed).
    ``key_of`` defaults to ``item.id``.
    """
    if key_of is None:
        key_of = _default_key_of
    collected: dict[str, T] = {}
    order: list[str] = []

    handler = _make_response_handler(
        fragment, extractor, collected, order, key_of=key_of
    )
    page.on("response", handler)

    try:
        await _sleep_jitter(PRE_NAV_DELAY_RANGE_S)
        await page.goto(initial_url, wait_until="domcontentloaded")
        if is_login_redirect(page.url):
            raise SessionExpiredError(_SESSION_EXPIRED_MSG)

        # The initial XHRs fire on navigation; give them a moment.
        await _sleep_jitter(SCROLL_DELAY_RANGE_S)
        if _should_stop(collected, limit):
            return _materialize(collected, order, limit)

        empty_streak = 0
        for _ in range(max_scrolls):
            before = len(collected)
            pixels = random.randint(*SCROLL_PIXELS_RANGE)
            await page.evaluate(f"window.scrollBy(0, {pixels})")
            await _sleep_jitter(SCROLL_DELAY_RANGE_S)
            if _should_stop(collected, limit):
                break
            if len(collected) == before:
                empty_streak += 1
                if empty_streak >= empty_streak_stop:
                    break
            else:
                empty_streak = 0
    finally:
        try:
            page.remove_listener("response", handler)
        except Exception:
            pass

    return _materialize(collected, order, limit)


def _should_stop(collected: dict[str, T], limit: int | None) -> bool:
    return limit is not None and len(collected) >= limit


def _materialize(
    collected: dict[str, T],
    order: list[str],
    limit: int | None,
) -> list[T]:
    """Return items in observed order, applying ``limit``."""
    items: list[T] = []
    for key in order:
        items.append(collected[key])
        if limit is not None and len(items) >= limit:
            break
    return items


async def intercept_single_response(
    page: "Page",
    initial_url: str,
    fragment: "str | tuple[str, ...]",
    extractor: Callable[[dict[str, Any]], "T | list[T] | None"],
    *,
    timeout_s: float | None = None,
) -> "T | list[T] | None":
    """Navigate once and return the first matching payload's parse.

    Used by endpoints that don't scroll (a single post, a profile header).
    The ``extractor`` may return ``None`` to reject a payload that matched the
    URL fragment but isn't the JSON we want (Instagram fires several GraphQL
    calls on a post page) — the handler then keeps waiting.

    Returns ``None`` if nothing usable was observed within ``timeout_s``.
    ``SINGLE_FETCH_TIMEOUT_S`` is looked up at call time so tests can patch it.
    """
    if timeout_s is None:
        timeout_s = SINGLE_FETCH_TIMEOUT_S
    result_holder: list[Any] = []
    done = asyncio.Event()

    async def on_response(response: "Response") -> None:
        if done.is_set():
            return
        if not _url_matches(response.url, fragment):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        parsed = extractor(payload)
        if parsed is None:
            return
        if isinstance(parsed, list) and not parsed:
            return
        result_holder.append(parsed)
        done.set()

    page.on("response", on_response)
    try:
        await _sleep_jitter(PRE_NAV_DELAY_RANGE_S)
        await page.goto(initial_url, wait_until="domcontentloaded")
        if is_login_redirect(page.url):
            raise SessionExpiredError(_SESSION_EXPIRED_MSG)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    return result_holder[0] if result_holder else None
