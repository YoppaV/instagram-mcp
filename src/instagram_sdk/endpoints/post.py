"""GET a single post by shortcode or URL → Post.

Unlike the feeds, a single post page server-renders its data into the HTML
(no separate XHR), so this navigates and digs the embedded JSON out.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ..models import Post
from ..parsers import find_embedded, parse_post
from ..scraper import fetch_rendered_html
from ._ids import EMBED_POST_KEY, extract_shortcode, post_url
from .profile import CLIPS_PRODUCT_TYPE, reel_play_counts

if TYPE_CHECKING:
    from playwright.async_api import Page

# The post page carries no play count; for a reel we look it up on the
# author's Reels tab, but only this far (~20 newest reels: 12 on load plus 3
# per scroll) — an old reel is not worth a long scroll.
REEL_LOOKUP_MAX_SCROLLS = 3


async def fetch(page: "Page", *, id_or_url: str) -> Post | None:
    """Return the ``Post`` for a shortcode/URL, or ``None`` if it didn't load."""
    shortcode = extract_shortcode(id_or_url)
    html = await fetch_rendered_html(page, post_url(shortcode))
    web_info = find_embedded(html, EMBED_POST_KEY)
    items = web_info.get("items") if isinstance(web_info, dict) else None
    if not items:
        return None
    result = parse_post(items[0])
    if (
        result is not None
        and result.product_type == CLIPS_PRODUCT_TYPE
        and result.view_count is None
        and result.author is not None
        and result.author.username
    ):
        counts = await reel_play_counts(
            page,
            username=result.author.username,
            shortcodes=[result.shortcode],
            max_scrolls=REEL_LOOKUP_MAX_SCROLLS,
        )
        if result.shortcode in counts:
            result = replace(result, view_count=counts[result.shortcode])
    return result
