"""Resolve which media items belong to a post — for the ``download_media`` tool.

Sits between the Playwright ``post.fetch`` endpoint and the ``download_media``
server tool. This layer answers *which URLs*; it never downloads bytes (that's
``downloader.fetch_bytes``).

The post is always re-fetched fresh. Instagram CDN URLs carry short-lived signed
query params (``oh`` / ``oe`` / ``_nc_*``) that expire within hours, so a URL
captured by an earlier ``get_post`` / ``get_saved_posts`` call may already be
dead — resolving here keeps the URL and the download in the same session.

A ``SessionExpiredError`` raised by ``post.fetch`` propagates unchanged, so the
tool surfaces the re-login instruction like every other one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import MediaItem
from . import post
from ._ids import extract_shortcode

if TYPE_CHECKING:
    from playwright.async_api import Page

# (shortcode, media)
Resolved = tuple[str, tuple[MediaItem, ...]]


async def resolve_post_media(page: "Page", *, id_or_url: str) -> Resolved:
    """Fetch a post fresh and return ``(shortcode, media)``.

    A carousel comes back as a ``Post`` whose ``media`` tuple holds one
    ``MediaItem`` per slide, so the tuple is already flat. Raises ``ValueError``
    if the post can't be loaded (deleted, private, or the page didn't render).
    """
    shortcode = extract_shortcode(id_or_url)
    result = await post.fetch(page, id_or_url=shortcode)
    if result is None:
        raise ValueError(
            f"Post {id_or_url!r} could not be loaded — it may be deleted, "
            "private, or the page failed to render."
        )
    return result.shortcode, result.media
