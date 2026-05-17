"""GET a single post by shortcode or URL → Post.

Unlike the feeds, a single post page server-renders its data into the HTML
(no separate XHR), so this navigates and digs the embedded JSON out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Post
from ..parsers import find_embedded, parse_post
from ..scraper import fetch_rendered_html
from ._ids import EMBED_POST_KEY, extract_shortcode, post_url

if TYPE_CHECKING:
    from playwright.async_api import Page


async def fetch(page: "Page", *, id_or_url: str) -> Post | None:
    """Return the ``Post`` for a shortcode/URL, or ``None`` if it didn't load."""
    shortcode = extract_shortcode(id_or_url)
    html = await fetch_rendered_html(page, post_url(shortcode))
    web_info = find_embedded(html, EMBED_POST_KEY)
    items = web_info.get("items") if isinstance(web_info, dict) else None
    if not items:
        return None
    return parse_post(items[0])
