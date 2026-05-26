"""GET a user's profile header, their posts, or their reels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Post, Profile, Reel
from ..parsers import extract_profile, extract_profile_posts, extract_reels
from ..scraper import intercept_single_response, scroll_collect
from ._ids import (
    FN_PROFILE,
    FN_PROFILE_POSTS,
    FN_REELS,
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
        FN_PROFILE,
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

    The grid mixes regular posts and reels; reels are excluded unless
    ``include_reels`` is set. Excluded reels are scrolled past, so ``limit``
    still yields that many actual posts.
    """
    handle = normalize_username(username)
    keep = None if include_reels else _is_not_reel
    return await scroll_collect(
        page,
        profile_url(handle),
        FN_PROFILE_POSTS,
        extract_profile_posts,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda post: post.id,
        keep=keep,
    )


def _is_not_reel(post: Post) -> bool:
    return post.product_type != CLIPS_PRODUCT_TYPE


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
        FN_REELS,
        extract_reels,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda reel: reel.id,
    )
