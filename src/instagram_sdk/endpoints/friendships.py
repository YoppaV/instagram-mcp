"""A user's followers and the accounts they follow.

REST ``/api/v1/friendships/<id>/followers/`` and ``/following/``, 12 users per
page like the web, driven by Instagram's ``next_max_id`` (opaque on
followers, a numeric offset on following) inside the usual opaque cursor.

This is the operation that draws the most blocks (``feedback_required``,
"Try again later"), so it is held tighter than the rest (design §10.3):

* at most ``FOLLOW_MAX_PAGES`` pages (60 users) per call — the walk never
  chains on its own; the caller decides whether to pass ``next_cursor`` back;
* its own budget class ``follow`` (30 pages per hour, 100 per day per
  account), slower pauses between pages and 30 s between calls (``pacer``);
* a private account the viewer does not follow is refused before the list
  request goes out (``PrivateAccountError``).

Exporting the followers of large accounts is not a goal.
"""

from __future__ import annotations

from typing import Any

from ..cursors import CursorState, PageResult, paginate
from ..errors import InstagramError
from ..models import User
from ..parsers import parse_user, rest_cursor
from ._ids import normalize_username
from .users import resolve_user_id

FOLLOW_PER_PAGE = 12
FOLLOW_MAX_PAGES = 5
KINDS = ("followers", "following")


class PrivateAccountError(InstagramError, ValueError):
    """The target account is private and the viewer does not follow it."""


def follow_users(payload: Any) -> list[User]:
    """``users[]`` of a followers/following page → ``User`` (no empty rows)."""
    if not isinstance(payload, dict):
        return []
    out: list[User] = []
    for row in payload.get("users") or []:
        user = parse_user(row) if isinstance(row, dict) else None
        if user is not None and user.pk and user.username:
            out.append(user)
    return out


async def _check_visible(ig: Any, handle: str, pk: str) -> None:
    """Refuse a private account the viewer does not follow (one profile read).

    The viewer's own lists are always visible, so they skip the check. The
    own pk comes from ``viewer_id()`` (page HTML, then cookie, then the stored
    id), never from ``actorID``, which is a Facebook-side id.
    """
    viewer_id = getattr(ig, "viewer_id", None)
    if viewer_id is not None and str(await viewer_id() or "") == pk:
        return
    user = await ig.graphql("profile_info", id=pk)
    if not isinstance(user, dict):
        raise ValueError(f"Instagram user @{handle} not found.")
    if not user.get("is_private"):
        return
    status = user.get("friendship_status")
    if isinstance(status, dict) and status.get("following"):
        return
    raise PrivateAccountError(
        f"@{handle} is a private account that this account does not follow: "
        "its followers and following lists are not visible. No list request was made."
    )


async def fetch_follow_page(
    ig: Any,
    kind: str,
    *,
    username: str,
    limit: int | None,
    start: CursorState | None = None,
    max_pages: int = FOLLOW_MAX_PAGES,
) -> PageResult[User]:
    """One cursor-driven walk over ``followers`` or ``following`` of ``username``.

    The visibility check runs only on a fresh walk: a cursor comes from a call
    that already passed it.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    handle = normalize_username(username)
    pk = await resolve_user_id(ig, handle)
    if pk is None:
        raise ValueError(f"Instagram user @{handle} not found.")
    if start is None:
        await _check_visible(ig, handle, pk)

    async def fetch_page(upstream: str | None) -> tuple[list[User], str | None]:
        body = await ig.rest(kind, path_params={"user_id": pk}, query={"max_id": upstream})
        return follow_users(body), rest_cursor(body, more_key="has_more")

    return await paginate(
        fetch_page,
        limit=limit,
        max_pages=min(max(1, max_pages), FOLLOW_MAX_PAGES),
        start=start,
        key_of=lambda user: user.pk,
    )


async def fetch_followers_page(ig: Any, *, username: str, limit: int | None,
                               start: CursorState | None = None) -> PageResult[User]:
    return await fetch_follow_page(ig, "followers", username=username, limit=limit, start=start)


async def fetch_following_page(ig: Any, *, username: str, limit: int | None,
                               start: CursorState | None = None) -> PageResult[User]:
    return await fetch_follow_page(ig, "following", username=username, limit=limit, start=start)


async def fetch_followers(ig: Any, *, username: str, limit: int | None = 24) -> list[User]:
    """Library helper: the first users of ``username``'s followers (≤ 5 pages)."""
    return (await fetch_followers_page(ig, username=username, limit=limit)).items


async def fetch_following(ig: Any, *, username: str, limit: int | None = 24) -> list[User]:
    """Library helper: the first accounts ``username`` follows (≤ 5 pages)."""
    return (await fetch_following_page(ig, username=username, limit=limit)).items
