"""The signed-in user's saved posts and save-collections.

Nothing here uses the handle any more: "all saved" is the REST feed
``/api/v1/feed/saved/posts/`` and collections are ``viewer`` GraphQL
queries, both scoped by the session cookies. A typo in the handle can no
longer turn into an empty list. ``handle=`` is still accepted (and ignored)
so library callers keep working.
"""

from __future__ import annotations

import re
from typing import Any

from ..cursors import (
    MAX_PAGES_PER_CALL,
    CursorState,
    PageResult,
    items_or_raise,
    page_budget,
    paginate,
)
from ..errors import EndpointGoneError, OperationFailedError
from ..models import Collection, Post
from ..parsers import connection_collections, connection_cursor, connection_posts, extract_saved, rest_cursor

ALL_MEDIA_COLLECTION_ID = "ALL_MEDIA_AUTO_COLLECTION"
_COLLECTION_ID_RE = re.compile(r"^\d{1,30}$")
SAVED_PER_PAGE = 21
COLLECTION_PER_PAGE = 12
MAX_COLLECTION_LIST_PAGES = 5
_GQL_PREFIX = "gql:"


async def fetch_saved_page(
    ig: Any,
    *,
    collection_id: str | None = None,
    limit: int | None,
    start: CursorState | None = None,
    max_pages: int = MAX_PAGES_PER_CALL,
) -> PageResult[Post]:
    """One cursor-driven walk over "All saved" or one collection."""
    if not collection_id or collection_id == ALL_MEDIA_COLLECTION_ID:

        async def fetch_page(upstream: str | None) -> tuple[list[Post], str | None]:
            body = await ig.rest("saved_posts", query={"max_id": upstream})
            return extract_saved(body), rest_cursor(body)

    else:
        cid = str(collection_id).strip()
        if not _COLLECTION_ID_RE.match(cid):
            raise ValueError(
                f"Invalid collection_id {collection_id!r}: use a numeric id from "
                "get_saved_collections (or ALL_MEDIA_AUTO_COLLECTION)."
            )
        # REST collection feed first: it still answers and, unlike the GraphQL
        # page the web uses now, carries video_versions and taken_at (D3/R3).
        # If it is gone or refuses the id, the walk switches to GraphQL; the
        # choice is made on the first page and GraphQL cursors carry a
        # ``gql:`` prefix, so a resumed walk never mixes the two.

        async def fetch_page(upstream: str | None) -> tuple[list[Post], str | None]:
            if upstream is None or not upstream.startswith(_GQL_PREFIX):
                try:
                    body = await ig.rest(
                        "collection_feed",
                        path_params={"collection_id": cid},
                        query={"max_id": upstream},
                    )
                    return extract_saved(body), rest_cursor(body)
                except (EndpointGoneError, OperationFailedError):
                    if upstream is not None:
                        raise
            after = upstream[len(_GQL_PREFIX):] if upstream else None
            root = await ig.graphql("saved_collection", collection_id=cid, after=after)
            if root is None:
                raise ValueError(
                    f"Saved collection {cid!r} does not exist. "
                    "List yours with get_saved_collections."
                )
            media = root.get("media") if isinstance(root, dict) else None
            nxt = connection_cursor(media)
            return connection_posts(media), (_GQL_PREFIX + nxt) if nxt else None

    return await paginate(
        fetch_page,
        limit=limit,
        max_pages=max_pages,
        start=start,
        key_of=lambda post: post.id,
    )


async def fetch_saved(
    ig: Any,
    *,
    handle: str | None = None,
    collection_id: str | None = None,
    limit: int | None = None,
) -> list[Post]:
    """Saved posts — the whole "All posts" bucket or one collection."""
    per_page = SAVED_PER_PAGE if not collection_id else COLLECTION_PER_PAGE
    page = await fetch_saved_page(
        ig,
        collection_id=collection_id,
        limit=limit,
        max_pages=page_budget(limit, per_page),
    )
    return items_or_raise(page)


async def fetch_collections(ig: Any, *, handle: str | None = None) -> list[Collection]:
    """List the user's save-collections (including the implicit "All posts")."""
    out: list[Collection] = []
    seen: set[str] = set()
    after: str | None = None
    for _ in range(MAX_COLLECTION_LIST_PAGES):
        root = await ig.graphql("saved_collections", after=after)
        for collection in connection_collections(root):
            if collection.collection_id not in seen:
                seen.add(collection.collection_id)
                out.append(collection)
        after = connection_cursor(root)
        if after is None:
            break
    return out
