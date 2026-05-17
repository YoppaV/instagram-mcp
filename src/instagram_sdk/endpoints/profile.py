"""GET a user's profile header, their posts, or their reels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Post, Profile, Reel
from ..parsers import extract_profile, extract_profile_posts, extract_reels
from ..scraper import intercept_single_response, scroll_collect
from ._ids import (
    FR_CLIPS,
    FR_USER_FEED,
    FR_WEB_PROFILE,
    normalize_username,
    profile_url,
    reels_url,
    scroll_budget,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

CLIPS_PRODUCT_TYPE = "clips"


async def fetch_profile(page: "Page", *, username: str) -> Profile | None:
    """Return the ``Profile`` header for ``username`` (bio, counts, flags)."""
    handle = normalize_username(username)
    result = await intercept_single_response(
        page,
        profile_url(handle),
        FR_WEB_PROFILE,
        extract_profile,
    )
    return result if isinstance(result, Profile) else None


async def fetch_posts(
    page: "Page",
    *,
    username: str,
    limit: int | None = None,
    include_reels: bool = False,
) -> list[Post]:
    """Scroll a profile's grid.

    Posts come from two sources on the same page: the first ~12 are embedded
    in the ``web_profile_info`` response, the rest from the ``feed/user``
    pagination endpoint — both fragments are intercepted and merged (deduped
    by ``Post.id``). Reels are excluded unless ``include_reels`` is set.
    """
    handle = normalize_username(username)
    posts = await scroll_collect(
        page,
        profile_url(handle),
        (FR_WEB_PROFILE, FR_USER_FEED),
        extract_profile_posts,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda post: post.id,
    )
    if not include_reels:
        posts = [p for p in posts if p.product_type != CLIPS_PRODUCT_TYPE]
    if limit is not None:
        posts = posts[:limit]
    return posts


async def fetch_reels(
    page: "Page",
    *,
    username: str,
    limit: int | None = None,
) -> list[Reel]:
    """Scroll a profile's Reels tab."""
    handle = normalize_username(username)
    return await scroll_collect(
        page,
        reels_url(handle),
        FR_CLIPS,
        extract_reels,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda reel: reel.id,
    )
