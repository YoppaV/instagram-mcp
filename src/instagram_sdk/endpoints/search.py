"""Search: accounts by text, and posts by hashtag.

* Users: the search box query (``search_box``, one request), the same one
  ``users.resolve_user_id`` uses. Its rows carry no ``is_private``.
* Hashtag: the classic tag page is gone (``/explore/tags/<tag>/`` redirects
  to the keyword search), so a tag is the keyword search for ``#tag``:
  ``keyword_first`` then ``keyword_page`` by cursor. Each result unit holds a
  few media (3 per unit, 24 per page in the capture). The web sends one uuid4
  as both ``search_session_id`` and ``serp_session_id`` on every page of a
  search; it travels inside our cursor with Instagram's own.

Both count in the ``search`` budget class, which is small (30/hour), and a
hashtag walk is capped at ``HASHTAG_MAX_PAGES`` per call.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..cursors import CursorState, PageResult, paginate
from ..models import Post, User
from ..parsers import connection_cursor, parse_post, search_users as _search_rows

MAX_QUERY_LEN = 100
HASHTAG_PAGE_SIZE = 24
HASHTAG_MAX_PAGES = 3

# Letters (any script), digits and underscore, like Instagram's own tags.
_TAG_RE = re.compile(r"^\w{1,100}$", re.UNICODE)


def normalize_query(query: str) -> str:
    text = " ".join((query or "").split())
    if not text:
        raise ValueError("Empty search query.")
    if len(text) > MAX_QUERY_LEN:
        raise ValueError(f"Search query too long (max {MAX_QUERY_LEN} characters).")
    return text


def normalize_tag(tag: str) -> str:
    """``#Travel`` / ``travel`` → ``travel``. ``ValueError`` if not a tag."""
    raw = (tag or "").strip().lstrip("#").strip()
    if not _TAG_RE.match(raw):
        raise ValueError(
            f"Invalid hashtag {tag!r}: letters, digits and underscores only, "
            "without spaces (the leading '#' is optional)."
        )
    return raw.casefold()


async def search_users(ig: Any, *, query: str, limit: int | None = 10) -> list[User]:
    """Accounts matching ``query``, in Instagram's ranking. One request."""
    text = normalize_query(query.strip().lstrip("@") if isinstance(query, str) else query)
    root = await ig.graphql("search_box", query=text)
    users: list[User] = []
    seen: set[str] = set()
    for user in _search_rows(root):
        if not user.pk or user.pk in seen:
            continue
        seen.add(user.pk)
        users.append(user)
    return users[:limit] if limit is not None else users


def connection_serp_posts(connection: Any) -> list[Post]:
    """Keyword-search connection → posts (``edges[].node.items[]``)."""
    out: list[Post] = []
    if not isinstance(connection, dict):
        return out
    for edge in connection.get("edges") or []:
        node = (edge or {}).get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        for item in node.get("items") or []:
            post = parse_post(item) if isinstance(item, dict) else None
            if post is not None:
                out.append(post)
    return out


def _pack(session_id: str, after: str | None) -> str:
    return json.dumps({"sid": session_id, "after": after}, separators=(",", ":"))


def _unpack(upstream: str) -> tuple[str, str | None]:
    try:
        data = json.loads(upstream)
        sid, after = data["sid"], data.get("after")
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid cursor: omit it to start over") from None
    if not isinstance(sid, str) or not sid or (after is not None and not isinstance(after, str)):
        raise ValueError("invalid cursor: omit it to start over")
    return sid, after


async def fetch_hashtag_page(
    ig: Any,
    *,
    tag: str,
    limit: int | None,
    start: CursorState | None = None,
    max_pages: int = HASHTAG_MAX_PAGES,
) -> PageResult[Post]:
    """Posts for ``#tag`` from the keyword search, by Instagram's ranking.

    The cursor's upstream is ``{"sid": <search session uuid>, "after": <cursor>}``
    so that a resumed walk keeps the session of its first page.
    """
    query = "#" + normalize_tag(tag)
    if start is None or start.upstream is None:
        start = CursorState(_pack(str(uuid.uuid4()), None), start.skip if start else 0)
    else:
        _unpack(start.upstream)  # reject a malformed cursor before any request

    async def fetch_page(upstream: str) -> tuple[list[Post], str | None]:
        sid, after = _unpack(upstream)
        if after is None:
            conn = await ig.graphql("keyword_first", query=query, session_id=sid)
        else:
            conn = await ig.graphql("keyword_page", query=query, session_id=sid, after=after)
        nxt = connection_cursor(conn)
        return connection_serp_posts(conn), (_pack(sid, nxt) if nxt else None)

    return await paginate(
        fetch_page,
        limit=limit,
        max_pages=max_pages,
        start=start,
        key_of=lambda post: post.id,
    )


async def fetch_hashtag(ig: Any, *, tag: str, limit: int | None = 24) -> list[Post]:
    """Library form without a cursor (same page cap)."""
    page = await fetch_hashtag_page(ig, tag=tag, limit=limit)
    return page.items
