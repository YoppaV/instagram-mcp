"""GET https://www.instagram.com/ → list[Post] (the home feed)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Post
from ..parsers import extract_home
from ..scraper import scroll_collect
from ._ids import BASE, FR_TIMELINE, scroll_budget

if TYPE_CHECKING:
    from playwright.async_api import Page

URL = BASE + "/"
FRAGMENT = FR_TIMELINE


async def fetch(page: "Page", *, limit: int | None = None) -> list[Post]:
    """Scroll the signed-in home timeline. Sponsored items are dropped."""
    return await scroll_collect(
        page,
        URL,
        FRAGMENT,
        extract_home,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda post: post.id,
    )
