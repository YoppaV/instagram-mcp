"""GET the signed-in user's saved posts and save-collections.

Saved content is private to the account, so these endpoints navigate under
the authenticated user's own profile (``/<handle>/saved/...``). The handle is
the one the server derives from the loaded session file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Collection, Post
from ..parsers import extract_collections, extract_saved
from ..scraper import intercept_single_response, scroll_collect
from ._ids import (
    FN_COLLECTIONS,
    FR_COLLECTION_FEED,
    FR_SAVED,
    collection_url,
    normalize_username,
    saved_all_url,
    saved_url,
    scroll_budget,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


async def fetch_saved(
    page: "Page",
    *,
    handle: str,
    collection_id: str | None = None,
    limit: int | None = None,
) -> list[Post]:
    """Scroll saved posts — the whole "All Posts" bucket or one collection."""
    h = normalize_username(handle)
    if collection_id:
        url = collection_url(h, collection_id)
        fragment = FR_COLLECTION_FEED
    else:
        url = saved_all_url(h)
        fragment = FR_SAVED

    return await scroll_collect(
        page,
        url,
        fragment,
        extract_saved,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda post: post.id,
    )


async def fetch_collections(page: "Page", *, handle: str) -> list[Collection]:
    """List the user's save-collections (including the implicit "All Posts")."""
    h = normalize_username(handle)
    result = await intercept_single_response(
        page,
        saved_url(h),
        FN_COLLECTIONS,
        extract_collections,
    )
    return result if isinstance(result, list) else []
