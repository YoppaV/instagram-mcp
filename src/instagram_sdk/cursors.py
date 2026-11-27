"""Cursor-driven pagination loop. Pure.

The loop is governed by Instagram's own cursor, not by how many items pass a
filter: a run of pages with nothing that survives ``keep`` (a profile that is
almost all reels) no longer stops the search early. Hitting the page cap is
reported (``stopped="page_cap"`` plus the state to resume from) instead of
being a silent cut.

Tools expose ``CursorState`` as an opaque string (``encode_cursor``):
``base64url`` of a compact JSON ``{"v","t","a","q","u","s"}`` — version, tool,
account, an 8-hex hash of the query parameters, Instagram's own cursor and
how many items of that page were already returned. Nothing is kept on the
server: a cursor works after a restart and from another client. Decoding
checks tool, account and query, so a cursor cannot be replayed against a
different query.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
    # The budget error that ended the walk early (``stopped == "budget"``).
    error: BudgetExceededError | None = field(default=None, repr=False, compare=False)


def items_or_raise(page: "PageResult[T]") -> list[T]:
    """Items of a library (list-returning) call; never a silently short list.

    A walk the budget cut midway raises ``BudgetExceededError`` carrying the
    items read so far in ``partial`` and the resume point in ``next``. Tools
    use the ``PageResult`` directly and return the partial page + cursor.
    """
    if page.stopped == STOP_BUDGET:
        cause = page.error
        err = BudgetExceededError(
            (str(cause) if cause else "request budget reached")
            + f" (after {len(page.items)} items; they are in .partial)",
            retry_after_s=cause.retry_after_s if cause else 0.0,
            partial=list(page.items),
            next=page.next,
        )
        raise err from cause
    return page.items


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
        except BudgetExceededError as exc:
            if pages == 0:
                raise
            return PageResult(out, state, STOP_BUDGET, error=exc)
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


# --------------------------------------------------------------------------
# Opaque cursor
# --------------------------------------------------------------------------
CURSOR_VERSION = 1


def query_hash(params: dict[str, Any]) -> str:
    """8 hex of sha1 over the canonical JSON of the query-defining params."""
    canon = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:8]


def encode_cursor(tool: str, account: str, params: dict[str, Any],
                  state: CursorState | None) -> str | None:
    """Opaque string for ``state``; ``None`` when there is nothing more."""
    if state is None:
        return None
    payload = {
        "v": CURSOR_VERSION,
        "t": tool,
        "a": account.casefold(),
        "q": query_hash(params),
        "u": state.upstream,
        "s": int(state.skip),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, tool: str, account: str,
                  params: dict[str, Any]) -> CursorState | None:
    """Back to ``CursorState``. ``ValueError`` if it is not one of ours or
    belongs to another tool, account or query."""
    if cursor is None or not str(cursor).strip():
        return None
    text = str(cursor).strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid cursor: pass back the next_cursor of a previous call "
                         "unchanged, or omit it to start over") from None
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ValueError("invalid or outdated cursor: omit it to start over")
    if (payload.get("t") != tool or payload.get("a") != account.casefold()
            or payload.get("q") != query_hash(params)):
        raise ValueError(
            "cursor from another query (different tool, account or parameters): "
            "call again with the same parameters, or omit the cursor to start over"
        )
    upstream = payload.get("u")
    skip = payload.get("s", 0)
    if upstream is not None and not isinstance(upstream, str):
        raise ValueError("invalid cursor: omit it to start over")
    if not isinstance(skip, int) or skip < 0:
        raise ValueError("invalid cursor: omit it to start over")
    return CursorState(upstream, skip)


def envelope(page: PageResult[Any], *, tool: str, account: str,
             params: dict[str, Any], serialize: Callable[[Any], Any]) -> dict[str, Any]:
    """What a paginated tool returns: ``{items, next_cursor, stopped}``."""
    return {
        "items": serialize(page.items),
        "next_cursor": encode_cursor(tool, account, params, page.next),
        "stopped": page.stopped,
    }
