"""Generic response-intercept + scroll loop reused by every list endpoint.

The shape: navigate to an instagram.com URL, install a
``page.on("response", ...)`` handler that recognizes the JSON responses we
care about, and scroll until the limit is reached or the feed goes quiet.
Instagram fires its own signed requests as you scroll — we never craft any.

Recognizing a response:
* GraphQL operations all POST to ``/api/graphql``; they are told apart by the
  ``fb_api_req_friendly_name`` field in the request body.
* A few feeds are plain REST endpoints with a distinctive URL path.

A single ``fragment`` string covers both — it matches if it equals the
request's friendly name OR appears in the response URL.

Single-fetch endpoints use ``intercept_single_response``; a single post page
is server-rendered, so ``fetch_rendered_html`` just returns its HTML.

Pacing is deliberately slow — Instagram detects automation aggressively, so
every delay range here is conservative and randomized. Do not tighten them.
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
# Jump to the bottom each step — that is what reliably drives Instagram's
# infinite scroll; small fixed increments often never trip the loader.
_SCROLL_TO_BOTTOM = "window.scrollTo(0, document.body.scrollHeight)"
DEFAULT_MAX_SCROLLS = 60
EMPTY_SCROLLS_BEFORE_STOP = 4
SINGLE_FETCH_TIMEOUT_S = 25.0

_RELOGIN_HINT = "re-run: python -m scripts.auth_login"
_SESSION_EXPIRED_MSG = f"Instagram session expired — {_RELOGIN_HINT}"
_FRIENDLY_NAME_FIELD = "fb_api_req_friendly_name="

T = TypeVar("T")
Extractor = Callable[[dict[str, Any]], list[T]]


def _default_key_of(item: Any) -> str:
    """Default key extractor: use ``id`` (stable across Post/Reel shapes)."""
    return getattr(item, "id", "") or ""


def _friendly_name(response: "Response") -> str:
    """The ``fb_api_req_friendly_name`` of the request that produced ``response``."""
    try:
        post_data = response.request.post_data
    except Exception:
        return ""
    if not post_data:
        return ""
    for part in post_data.split("&"):
        if part.startswith(_FRIENDLY_NAME_FIELD):
            return part[len(_FRIENDLY_NAME_FIELD):]
    return ""


def _matches(response: "Response", fragment: "str | tuple[str, ...]") -> bool:
    """True if ``fragment`` names this response (by URL path or friendly name)."""
    fragments = (fragment,) if isinstance(fragment, str) else tuple(fragment)
    url = response.url
    friendly: str | None = None
    for frag in fragments:
        if frag in url:
            return True
        if friendly is None:
            friendly = _friendly_name(response)
        if frag and frag == friendly:
            return True
    return False


def _make_response_handler(
    fragment: "str | tuple[str, ...]",
    extractor: Extractor[T],
    collected: dict[str, T],
    order: list[str],
    key_of: Callable[[T], str],
    keep: Callable[[T], bool] | None,
) -> Callable[["Response"], Awaitable[None]]:
    async def on_response(response: "Response") -> None:
        if not _matches(response, fragment):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        for item in extractor(payload):
            if keep is not None and not keep(item):
                continue
            key = key_of(item)
            if not key or key in collected:
                continue
            collected[key] = item
            order.append(key)

    return on_response


async def _sleep_jitter(rng: tuple[float, float]) -> None:
    await asyncio.sleep(random.uniform(*rng))


def _seed(
    items: list[T],
    collected: dict[str, T],
    order: list[str],
    key_of: Callable[[T], str],
    keep: Callable[[T], bool] | None,
) -> None:
    for item in items:
        if keep is not None and not keep(item):
            continue
        key = key_of(item)
        if key and key not in collected:
            collected[key] = item
            order.append(key)


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
    keep: Callable[[T], bool] | None = None,
    embed_extractor: Callable[[str], list[T]] | None = None,
) -> list[T]:
    """Scroll ``initial_url`` and collect items parsed from ``fragment``.

    Stops when ``limit`` is reached, when scrolling produces no new items for
    ``empty_streak_stop`` iterations, or when ``max_scrolls`` is exhausted.
    ``key_of`` defaults to ``item.id``. ``keep``, if given, filters items
    before they count toward ``limit`` (skipped items are still scrolled
    past). ``embed_extractor`` is run once over the landing page's HTML to
    seed items Instagram server-renders instead of fetching.
    """
    if key_of is None:
        key_of = _default_key_of
    collected: dict[str, T] = {}
    order: list[str] = []

    handler = _make_response_handler(
        fragment, extractor, collected, order, key_of=key_of, keep=keep
    )
    page.on("response", handler)

    try:
        await _sleep_jitter(PRE_NAV_DELAY_RANGE_S)
        await page.goto(initial_url, wait_until="domcontentloaded")
        if is_login_redirect(page.url):
            raise SessionExpiredError(_SESSION_EXPIRED_MSG)

        if embed_extractor is not None:
            try:
                html = await page.content()
            except Exception:
                html = ""
            _seed(embed_extractor(html), collected, order, key_of, keep)

        # The initial requests fire on navigation; give them a moment.
        await _sleep_jitter(SCROLL_DELAY_RANGE_S)
        if _should_stop(collected, limit):
            return _materialize(collected, order, limit)

        empty_streak = 0
        for _ in range(max_scrolls):
            before = len(collected)
            await page.evaluate(_SCROLL_TO_BOTTOM)
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

    The ``extractor`` may return ``None`` to reject a response that matched
    the fragment but isn't the JSON we want (Instagram fires many GraphQL
    calls per page) — the handler then keeps waiting.

    Returns ``None`` if nothing usable was observed within ``timeout_s``.
    ``SINGLE_FETCH_TIMEOUT_S`` is looked up at call time so tests can patch it.
    """
    if timeout_s is None:
        timeout_s = SINGLE_FETCH_TIMEOUT_S
    result_holder: list[Any] = []
    done = asyncio.Event()

    async def on_response(response: "Response") -> None:
        if done.is_set() or not _matches(response, fragment):
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


async def fetch_rendered_html(page: "Page", url: str) -> str:
    """Navigate to ``url`` and return its server-rendered HTML.

    Used for the single-post page, whose data Instagram embeds in the HTML
    rather than serving over a separate request.
    """
    await _sleep_jitter(PRE_NAV_DELAY_RANGE_S)
    await page.goto(url, wait_until="domcontentloaded")
    if is_login_redirect(page.url):
        raise SessionExpiredError(_SESSION_EXPIRED_MSG)
    await _sleep_jitter(SCROLL_DELAY_RANGE_S)
    return await page.content()
