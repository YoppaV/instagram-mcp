"""GET a single post by shortcode or URL → Post."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Post
from ..parsers import extract_post_detail
from ..scraper import intercept_single_response
from ._ids import FR_GRAPHQL, extract_shortcode, post_url

if TYPE_CHECKING:
    from playwright.async_api import Page

FRAGMENT = FR_GRAPHQL


async def fetch(page: "Page", *, id_or_url: str) -> Post | None:
    """Return the ``Post`` for a shortcode/URL, or ``None`` if it didn't load.

    A post page fires several ``/graphql/query`` calls; ``extract_post_detail``
    only accepts the one carrying ``xdt_shortcode_media``, so the interceptor
    keeps waiting past the noise.
    """
    shortcode = extract_shortcode(id_or_url)
    result = await intercept_single_response(
        page,
        post_url(shortcode),
        FRAGMENT,
        extract_post_detail,
    )
    return result if isinstance(result, Post) else None
