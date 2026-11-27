"""A user's profile header, their grid posts, and their reels.

* Profile: username → id (``users.resolve_user_id``) → ``profile_info``.
* Grid: ``profile_posts_first`` then ``profile_posts_page`` by cursor, by
  username. Reels are filtered out unless asked for, and the walk is driven
  by Instagram's cursor, so a run of reels no longer ends it early.
* Reels tab: needs the id; it is the only surface with ``play_count``, so it
  also backs the best-effort view counts of reels found elsewhere.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, Iterable

from ..cursors import MAX_PAGES_PER_CALL, CursorState, PageResult, page_budget, paginate
from ..errors import AccountBlockedError, SessionExpiredError
from ..models import Post, Profile, Reel
from ..parsers import connection_cursor, connection_posts, connection_reels, parse_profile
from ._ids import normalize_username
from .users import resolve_user_id

log = logging.getLogger("instagram_sdk")

CLIPS_PRODUCT_TYPE = "clips"
GRID_PER_PAGE = 12
# The web pages the Reels tab 3 at a time; 12 asks 4x fewer requests (D4).
REELS_PAGE_SIZE = 12
# How many pages past the first to look for a reel's play count.
REEL_LOOKUP_MAX_PAGES = 2
PLAYS_TTL_S = 600


def _is_not_reel(post: Post) -> bool:
    return post.product_type != CLIPS_PRODUCT_TYPE


async def fetch_profile(ig: Any, *, username: str) -> Profile | None:
    """``Profile`` header for ``username``; ``None`` if it does not exist."""
    handle = normalize_username(username)
    for attempt in range(2):
        pk = await resolve_user_id(ig, handle, use_cache=attempt == 0)
        if pk is None:
            return None
        user = await ig.graphql("profile_info", id=pk)
        profile = parse_profile(user) if isinstance(user, dict) else None
        if profile is None:
            return None
        if profile.username.casefold() == handle.casefold():
            return profile
        # Renamed account: the cached id now belongs to someone else.
        cache = getattr(ig, "user_ids", None)
        if cache is not None:
            cache.drop(handle)
    return None


async def fetch_posts_page(
    ig: Any,
    *,
    username: str,
    limit: int | None,
    include_reels: bool = False,
    start: CursorState | None = None,
    max_pages: int = MAX_PAGES_PER_CALL,
) -> PageResult[Post]:
    handle = normalize_username(username)

    async def fetch_page(upstream: str | None) -> tuple[list[Post], str | None]:
        if upstream is None:
            conn = await ig.graphql("profile_posts_first", username=handle)
        else:
            conn = await ig.graphql("profile_posts_page", username=handle, after=upstream)
        return connection_posts(conn), connection_cursor(conn)

    page = await paginate(
        fetch_page,
        limit=limit,
        max_pages=max_pages,
        start=start,
        key_of=lambda post: post.id,
        keep=None if include_reels else _is_not_reel,
    )
    if include_reels:
        page.items = await _with_reel_play_counts(ig, handle, page.items)
    return page


async def fetch_posts(
    ig: Any,
    *,
    username: str,
    limit: int | None = None,
    include_reels: bool = False,
) -> list[Post]:
    """A profile's grid. Reels are excluded unless ``include_reels``."""
    page = await fetch_posts_page(
        ig,
        username=username,
        limit=limit,
        include_reels=include_reels,
        max_pages=page_budget(limit, GRID_PER_PAGE),
    )
    return page.items


async def _with_reel_play_counts(ig: Any, handle: str, posts: list[Post]) -> list[Post]:
    """Fill ``view_count`` on grid reels from the Reels tab (best effort)."""
    missing = [
        p for p in posts if p.product_type == CLIPS_PRODUCT_TYPE and p.view_count is None
    ]
    if not missing:
        return posts
    author_pk = next((p.author.pk for p in missing if p.author and p.author.pk), None)
    counts = await reel_play_counts(
        ig,
        username=handle,
        user_id=author_pk,
        shortcodes=[p.shortcode for p in missing],
        max_scrolls=REEL_LOOKUP_MAX_PAGES,
    )
    if not counts:
        return posts
    return [
        replace(p, view_count=counts[p.shortcode]) if p.shortcode in counts else p
        for p in posts
    ]


def _plays_cache(ig: Any, user_id: str) -> dict[str, int]:
    cache = getattr(ig, "cache", None)
    if not isinstance(cache, dict):
        return {}
    plays = cache.setdefault("plays", {})
    entry = plays.get(user_id)
    now = time.monotonic()
    if entry is None or now - entry[0] > PLAYS_TTL_S:
        entry = (now, {})
        plays[user_id] = entry
    return entry[1]


async def reel_play_counts(
    ig: Any,
    *,
    shortcodes: Iterable[str],
    username: str | None = None,
    user_id: str | None = None,
    max_scrolls: int = REEL_LOOKUP_MAX_PAGES,
) -> dict[str, int]:
    """Map shortcode → ``play_count`` for the given reels, best effort.

    Reads the author's Reels tab: the first page plus up to ``max_scrolls``
    more (the name is kept for library callers). Reels not found are left
    out. Any failure other than a block or an expired session is logged and
    yields what was found so far — view counts never fail a call.
    """
    wanted = {code for code in shortcodes if code}
    if not wanted:
        return {}
    try:
        pk = user_id or (await resolve_user_id(ig, username) if username else None)
        if not pk:
            return {}
        plays = _plays_cache(ig, pk)
        found = {code: plays[code] for code in wanted if code in plays}
        if len(found) == len(wanted):
            return found
        after: str | None = None
        for page_no in range(1 + max(0, max_scrolls)):
            if page_no == 0:
                conn = await ig.graphql("profile_reels_first", id=pk)
            else:
                conn = await ig.graphql(
                    "profile_reels_page", id=pk, after=after, first=REELS_PAGE_SIZE
                )
            for reel in connection_reels(conn):
                if reel.play_count is not None:
                    plays[reel.shortcode] = reel.play_count
                    if reel.shortcode in wanted:
                        found[reel.shortcode] = reel.play_count
            after = connection_cursor(conn)
            if len(found) == len(wanted) or after is None:
                break
        return found
    except (AccountBlockedError, SessionExpiredError):
        raise
    except Exception as exc:  # best effort: never fail the caller
        log.warning("instagram_sdk: reel view counts unavailable: %s", exc)
        return {}


async def fetch_reels_page(
    ig: Any,
    *,
    username: str,
    limit: int | None,
    start: CursorState | None = None,
    max_pages: int = MAX_PAGES_PER_CALL,
) -> PageResult[Reel]:
    handle = normalize_username(username)
    pk = await resolve_user_id(ig, handle)
    if pk is None:
        return PageResult([], None, "end")

    async def fetch_page(upstream: str | None) -> tuple[list[Reel], str | None]:
        if upstream is None:
            conn = await ig.graphql("profile_reels_first", id=pk)
        else:
            conn = await ig.graphql(
                "profile_reels_page", id=pk, after=upstream, first=REELS_PAGE_SIZE
            )
        return connection_reels(conn), connection_cursor(conn)

    return await paginate(
        fetch_page,
        limit=limit,
        max_pages=max_pages,
        start=start,
        key_of=lambda reel: reel.id,
    )


async def fetch_reels(ig: Any, *, username: str, limit: int | None = None) -> list[Reel]:
    """A profile's Reels tab, newest first."""
    page = await fetch_reels_page(
        ig, username=username, limit=limit, max_pages=page_budget(limit, REELS_PAGE_SIZE)
    )
    return page.items
