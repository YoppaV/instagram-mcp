"""The signed-in home timeline → list[Post].

First batch: the timeline connection embedded in the ``/`` HTML when the
server renders it (the fixed tab may already hold that HTML from its own
bootstrap); otherwise the web's first-page query. Following pages: the
pagination query, without ``feed_view_info`` ("seen" telemetry, D1).
Ads, suggestions and injected items are dropped.
"""

from __future__ import annotations

from typing import Any

from ..cursors import MAX_PAGES_PER_CALL, CursorState, PageResult, page_budget, paginate
from ..models import Post
from ..parsers import TIMELINE_CONNECTION_KEY, connection_cursor, find_embedded, home_posts

HOME_PAGE_SIZE = 12
RECENT_HTML_S = 10.0


async def _first_batch(ig: Any) -> tuple[list[Post], str | None]:
    html = None
    recent = getattr(ig, "recent_html", None)
    if callable(recent):
        html = recent("/", RECENT_HTML_S)
    if html is None:
        html = await ig.navigate_html("/")
    connection = find_embedded(html, TIMELINE_CONNECTION_KEY)
    if not isinstance(connection, dict):
        connection = await ig.graphql("home_first")
    return home_posts(connection), connection_cursor(connection)


async def fetch_home_page(
    ig: Any,
    *,
    limit: int | None,
    start: CursorState | None = None,
    max_pages: int = MAX_PAGES_PER_CALL,
) -> PageResult[Post]:
    async def fetch_page(upstream: str | None) -> tuple[list[Post], str | None]:
        if upstream is None:
            return await _first_batch(ig)
        connection = await ig.graphql("home_page", after=upstream, first=HOME_PAGE_SIZE)
        return home_posts(connection), connection_cursor(connection)

    return await paginate(
        fetch_page,
        limit=limit,
        max_pages=max_pages,
        start=start,
        key_of=lambda post: post.id,
        stop_when_no_new=True,
    )


async def fetch(ig: Any, *, limit: int | None = None) -> list[Post]:
    """Read the home timeline. Sponsored and suggested items are dropped."""
    page = await fetch_home_page(ig, limit=limit, max_pages=page_budget(limit, HOME_PAGE_SIZE))
    return page.items
