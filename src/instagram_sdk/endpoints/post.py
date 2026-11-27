"""A single post by shortcode or URL → Post.

The post page server-renders its data into the HTML (``EMBED_POST_KEY``), so
this loads ``/p/<code>/`` in the fixed tab — without its JS — and digs the
embedded JSON out. The GraphQL ``post_root`` operation is registered as an
alternative but not used (D2).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..models import Post
from ..parsers import find_embedded, parse_post
from ._ids import EMBED_POST_KEY, extract_shortcode
from .profile import CLIPS_PRODUCT_TYPE, REEL_LOOKUP_MAX_PAGES, reel_play_counts

POST_STRATEGY = ("html", "graphql")


async def fetch(ig: Any, *, id_or_url: str, with_views: bool = True) -> Post | None:
    """The ``Post`` for a shortcode/URL, or ``None`` if the page had no data.

    For a reel, ``view_count`` is looked up on the author's Reels tab (best
    effort, a few pages at most) unless ``with_views`` is False.
    """
    shortcode = extract_shortcode(id_or_url)
    html = await ig.navigate_html(f"/p/{shortcode}/")
    web_info = find_embedded(html, EMBED_POST_KEY)
    items = web_info.get("items") if isinstance(web_info, dict) else None
    if not items:
        return None
    result = parse_post(items[0])
    if (
        with_views
        and result is not None
        and result.product_type == CLIPS_PRODUCT_TYPE
        and result.view_count is None
        and result.author is not None
        and (result.author.pk or result.author.username)
    ):
        counts = await reel_play_counts(
            ig,
            username=result.author.username or None,
            user_id=result.author.pk or None,
            shortcodes=[result.shortcode],
            max_scrolls=REEL_LOOKUP_MAX_PAGES,
        )
        if result.shortcode in counts:
            result = replace(result, view_count=counts[result.shortcode])
    return result
