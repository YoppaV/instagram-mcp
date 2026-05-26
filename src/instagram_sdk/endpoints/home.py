"""GET https://www.instagram.com/ → list[Post] (the home feed)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Post
from ..parsers import extract_home, extract_home_html
from ..scraper import scroll_collect
from ._ids import BASE, FN_TIMELINE, scroll_budget

if TYPE_CHECKING:
    from playwright.async_api import Page

URL = BASE + "/"
FRAGMENT = FN_TIMELINE


async def fetch(page: "Page", *, limit: int | None = None) -> list[Post]:
    """Scroll the signed-in home timeline. Sponsored items are dropped.

    The first batch is server-rendered into the page HTML; later pages arrive
    over the pagination query — ``scroll_collect`` merges both.
    """
    return await scroll_collect(
        page,
        URL,
        FRAGMENT,
        extract_home,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda post: post.id,
        embed_extractor=extract_home_html,
    )
