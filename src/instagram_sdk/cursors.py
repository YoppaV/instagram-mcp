"""Cursor-driven pagination loop. Pure.

The loop is governed by Instagram's own cursor, not by how many items pass a
filter: a run of pages with nothing that survives ``keep`` (a profile that is
almost all reels) no longer stops the search early. Hitting the page cap is
reported (``stopped="page_cap"`` plus the state to resume from) instead of
being a silent cut.

The opaque cursor string exposed by tools arrives with multi-account support;
``CursorState`` is already the thing it will encode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, TypeVar

from .errors import BudgetExceededError

T = TypeVar("T")

MAX_PAGES_PER_CALL = 8
MAX_PAGES_CAP = 60

STOP_END = "end"
STOP_LIMIT = "limit"
STOP_PAGE_CAP = "page_cap"
STOP_BUDGET = "budget"


@dataclass(frozen=True)
class CursorState:
    upstream: str | None
    skip: int = 0


@dataclass
class PageResult(Generic[T]):
    items: list[T] = field(default_factory=list)
    next: CursorState | None = None
    stopped: str = STOP_END


FetchPage = Callable[[Any], Awaitable[tuple[list[T], Any]]]


def page_budget(limit: int | None, per_page: int) -> int:
    """Pages to allow a library call with no cursor for ``limit`` items."""
    if not limit or limit <= 0:
        return MAX_PAGES_CAP
    return min(MAX_PAGES_CAP, math.ceil(limit / max(1, per_page)) + 4)


def _default_key(item: Any) -> str:
    return getattr(item, "id", "") or ""


async def paginate(
    fetch_page: FetchPage,
    *,
    limit: int | None,
    max_pages: int,
    start: CursorState | None = None,
    key_of: Callable[[T], str] = _default_key,
    keep: Callable[[T], bool] | None = None,
    stop_when_no_new: bool = False,
) -> PageResult[T]:
    """Fetch pages until ``limit`` items, the end, or ``max_pages``.

    ``fetch_page(upstream)`` returns ``(items, next_upstream)``; ``next`` is
    ``None`` when Instagram says there is no next page. A budget error after at
    least one page becomes a partial result with ``stopped="budget"``.
    ``stop_when_no_new`` ends the walk when a page brings only duplicates
    (the home feed can loop).
    """
    state = start or CursorState(None, 0)
    out: list[T] = []
    seen: set[str] = set()
    pages = 0
    while True:
        try:
            raw, nxt = await fetch_page(state.upstream)
        except BudgetExceededError:
            if pages == 0:
                raise
            return PageResult(out, state, STOP_BUDGET)
        pages += 1
        new_keys = 0
        for i, item in enumerate(raw):
            if i < state.skip:
                continue
            key = key_of(item)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            new_keys += 1
            if keep is not None and not keep(item):
                continue
            out.append(item)
            if limit is not None and len(out) >= limit:
                if i + 1 < len(raw):
                    return PageResult(out, CursorState(state.upstream, i + 1), STOP_LIMIT)
                return PageResult(
                    out, CursorState(nxt, 0) if nxt else None, STOP_LIMIT
                )
        if nxt is None:
            return PageResult(out, None, STOP_END)
        if stop_when_no_new and new_keys == 0:
            return PageResult(out, None, STOP_END)
        if pages >= max_pages:
            return PageResult(out, CursorState(nxt, 0), STOP_PAGE_CAP)
        state = CursorState(nxt, 0)
