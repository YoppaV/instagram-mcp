"""A post's comments, and optionally the first replies of each.

* First page: ``comments_first`` (``PolarisPostCommentsContainerQuery``, 15
  comments), by numeric media id — decoded from the shortcode, no request.
* Next pages: ``comments_page`` (10 per page, ``sort_order="popular"`` like
  the web). Instagram's cursor is a JSON string; it travels inside our opaque
  cursor together with the ids of the latest comments returned, because the
  next page can repeat some of them.
* Replies (``include_replies``): one ``comment_replies`` request per comment
  that has any, for the first ``MAX_REPLY_THREADS`` such comments only, and at
  most ``MAX_REPLIES_PER_COMMENT`` replies kept from each. Replies are a cheap
  sample, not an export: every thread costs a request.

All three operations count in the ``comments`` budget class. A post that does
not exist (or exposes no comments) yields no items.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from ..cursors import MAX_PAGES_PER_CALL, CursorState, PageResult, page_budget, paginate
from ..errors import AccountBlockedError, BudgetExceededError, SessionExpiredError
from ..models import Comment
from ..parsers import connection_cursor, parse_user
from ._ids import extract_shortcode, shortcode_to_pk

FIRST_PAGE_SIZE = 15
PAGE_SIZE = 10
# Replies are a sample: at most this many threads per call, this many replies each.
MAX_REPLY_THREADS = 3
MAX_REPLIES_PER_COMMENT = 5

# Instagram's next page can repeat a few comments of the previous one (seen
# live: 2 of the first 15 came back on page 2). The ids of the last pages
# travel in the cursor so a later call drops them too.
SEEN_CARRIED = 30

# A numeric media id (``pk``) is ~19 digits; a shortcode is 11 base-64 chars.
_MEDIA_PK_RE = re.compile(r"^\d{15,25}$")


def _pack(after: str, seen: list[str]) -> str:
    return json.dumps({"after": after, "seen": seen}, separators=(",", ":"))


def _unpack(upstream: str) -> tuple[str, list[str]]:
    try:
        data = json.loads(upstream)
        after, seen = data["after"], data.get("seen") or []
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid cursor: omit it to start over") from None
    if not isinstance(after, str) or not isinstance(seen, list) \
            or not all(isinstance(pk, str) for pk in seen):
        raise ValueError("invalid cursor: omit it to start over")
    return after, seen


def media_id_of(id_or_url: str) -> str:
    """Numeric media id from a shortcode, a post/reel URL or the id itself."""
    raw = (id_or_url or "").strip()
    if _MEDIA_PK_RE.match(raw):
        return raw
    return shortcode_to_pk(extract_shortcode(raw))


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def parse_comment(node: dict[str, Any] | None) -> Comment | None:
    """An ``XIGComment`` node → ``Comment``; ``None`` if it has no id."""
    if not isinstance(node, dict):
        return None
    pk = str(node.get("pk") or node.get("id") or "")
    if not pk:
        return None
    parent = node.get("parent_comment_id")
    user = node.get("user")
    return Comment(
        pk=pk,
        text=node.get("text"),
        created_at=_int(node.get("created_at")),
        like_count=_int(node.get("comment_like_count")),
        reply_count=_int(node.get("child_comment_count")),
        parent_pk=str(parent) if parent else None,
        is_pinned=bool(node.get("is_pinned")),
        is_edited=bool(node.get("is_edited")),
        author=parse_user(user) if isinstance(user, dict) else None,
    )


def connection_comments(connection: Any) -> list[Comment]:
    """A comments (or child comments) connection → comments."""
    out: list[Comment] = []
    if not isinstance(connection, dict):
        return out
    for edge in connection.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        comment = parse_comment(edge.get("node"))
        if comment is not None:
            out.append(comment)
    return out


async def fetch_replies(ig: Any, *, comment_id: str,
                        limit: int = MAX_REPLIES_PER_COMMENT) -> list[Comment]:
    """First page of replies to ``comment_id``, at most ``limit``. One request."""
    conn = await ig.graphql("comment_replies", comment_id=str(comment_id), after=None)
    return connection_comments(conn)[: max(0, limit)]


async def _with_replies(ig: Any, comments: list[Comment]) -> list[Comment]:
    """Attach the first replies to the first threads that have any.

    Comments with no replies get ``replies=()``; threads past the cap, or
    whose lookup ran out of budget, keep ``replies=None`` (not fetched).
    """
    out: list[Comment] = []
    threads = 0
    stop = False
    for comment in comments:
        if not comment.reply_count:
            out.append(replace(comment, replies=()))
            continue
        if stop or threads >= MAX_REPLY_THREADS:
            out.append(comment)
            continue
        threads += 1
        try:
            replies = await fetch_replies(ig, comment_id=comment.pk)
        except BudgetExceededError:
            stop = True
            out.append(comment)
            continue
        except (AccountBlockedError, SessionExpiredError):
            raise
        out.append(replace(comment, replies=tuple(replies)))
    return out


async def fetch_comments_page(
    ig: Any,
    *,
    id_or_url: str,
    limit: int | None,
    include_replies: bool = False,
    start: CursorState | None = None,
    max_pages: int = MAX_PAGES_PER_CALL,
) -> PageResult[Comment]:
    """Top-level comments of a post, in Instagram's order ("popular")."""
    media_id = media_id_of(id_or_url)
    if start is not None and start.upstream is not None:
        _unpack(start.upstream)  # reject a malformed cursor before any request

    async def fetch_page(upstream: str | None) -> tuple[list[Comment], str | None]:
        seen: list[str] = []
        if upstream is None:
            conn = await ig.graphql("comments_first", media_id=media_id)
        else:
            after, seen = _unpack(upstream)
            conn = await ig.graphql("comments_page", media_id=media_id, after=after)
        skip = set(seen)
        items = [c for c in connection_comments(conn) if c.pk not in skip]
        nxt = connection_cursor(conn)
        if nxt is None:
            return items, None
        recent = (seen + [c.pk for c in items])[-SEEN_CARRIED:]
        return items, _pack(nxt, recent)

    page = await paginate(
        fetch_page,
        limit=limit,
        max_pages=max_pages,
        start=start,
        key_of=lambda c: c.pk,
    )
    if include_replies and page.items:
        page.items = await _with_replies(ig, page.items)
    return page


async def fetch_comments(ig: Any, *, id_or_url: str, limit: int | None = 20,
                         include_replies: bool = False) -> list[Comment]:
    """Library form without a cursor."""
    page = await fetch_comments_page(
        ig, id_or_url=id_or_url, limit=limit, include_replies=include_replies,
        max_pages=page_budget(limit, PAGE_SIZE),
    )
    return page.items
