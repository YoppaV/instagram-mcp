"""Resolve which media items belong to a post — for the ``download_media`` tool.

This layer answers *which URLs*; it never downloads bytes (that is
``downloader.fetch_bytes``). The post is always re-fetched fresh: Instagram
CDN URLs carry short-lived signed query params, so a URL captured earlier may
already be dead.
"""

from __future__ import annotations

from typing import Any

from ..models import MediaItem
from . import post
from ._ids import extract_shortcode

# (shortcode, media)
Resolved = tuple[str, tuple[MediaItem, ...]]


async def resolve_post_media(ig: Any, *, id_or_url: str) -> Resolved:
    """Fetch a post fresh and return ``(shortcode, media)``.

    Raises ``ValueError`` if the post can't be loaded (deleted, private, or
    the page carried no data).
    """
    shortcode = extract_shortcode(id_or_url)
    result = await post.fetch(ig, id_or_url=shortcode, with_views=False)
    if result is None:
        raise ValueError(
            f"Post {id_or_url!r} could not be loaded — it may be deleted, "
            "private, or the page carried no post data."
        )
    return result.shortcode, result.media
