"""GET a user's profile header, their posts, or their reels."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

from ..models import Post, Profile, Reel
from ..parsers import extract_profile, extract_profile_posts, extract_reels
from ..scraper import intercept_single_response, scroll_collect
from ._ids import (
    FN_PROFILE,
    FN_PROFILE_POSTS,
    FN_REELS,
    REELS_PER_SCROLL,
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
    posts = await scroll_collect(
        page,
        profile_url(handle),
        FN_PROFILE_POSTS,
        extract_profile_posts,
        limit=limit,
        max_scrolls=scroll_budget(limit),
        key_of=lambda post: post.id,
        keep=keep,
    )
    if include_reels:
        posts = await _with_reel_play_counts(page, handle, posts)
    return posts


async def _with_reel_play_counts(
    page: "Page", handle: str, posts: list[Post]
) -> list[Post]:
    """Fill ``view_count`` on grid reels from the Reels tab.

    The grid query no longer selects any play count, so reels in the grid come
    back with ``view_count=None``. The Reels tab does carry ``play_count``;
    scroll it just far enough to cover the reels we already have.
    """
    missing = [
        p.shortcode
        for p in posts
        if p.product_type == CLIPS_PRODUCT_TYPE and p.view_count is None
    ]
    if not missing:
        return posts
    counts = await reel_play_counts(
        page,
        username=handle,
        shortcodes=missing,
        max_scrolls=scroll_budget(len(missing), REELS_PER_SCROLL),
    )
    if not counts:
        return posts
    return [
        replace(p, view_count=counts[p.shortcode]) if p.shortcode in counts else p
        for p in posts
    ]


async def reel_play_counts(
    page: "Page",
    *,
    username: str,
    shortcodes: Iterable[str],
    max_scrolls: int,
) -> dict[str, int]:
    """Map shortcode → ``play_count`` for the given reels of ``username``.

    Scrolls the Reels tab until every shortcode is found, the tab goes quiet,
    or ``max_scrolls`` runs out. Reels not found are simply left out.
    """
    wanted = {code for code in shortcodes if code}
    if not wanted:
        return {}
    reels = await scroll_collect(
        page,
        reels_url(normalize_username(username)),
        FN_REELS,
        extract_reels,
        limit=len(wanted),
        max_scrolls=max_scrolls,
        key_of=lambda reel: reel.shortcode,
        keep=lambda reel: reel.shortcode in wanted and reel.play_count is not None,
    )
    return {reel.shortcode: reel.play_count for reel in reels if reel.play_count is not None}


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
        max_scrolls=scroll_budget(limit, REELS_PER_SCROLL),
        key_of=lambda reel: reel.id,
    )
