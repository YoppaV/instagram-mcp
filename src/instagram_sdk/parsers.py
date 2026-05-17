"""Pure extractors that turn Instagram JSON payloads into our dataclasses.

No Playwright / IO here. Functions accept dicts, return dataclasses, ``None``,
or lists. This module is exhaustively unit-testable with captured fixtures.

Instagram serves two unrelated JSON shapes for the same underlying objects:

* **private-API media** — from ``/api/v1/feed/...``, ``/clips/user/``,
  ``/feed/saved/...``: integer ``media_type`` (1/2/8), ``code``,
  ``image_versions2.candidates``, ``caption.text``, ``like_count``.
* **GraphQL node** — from ``users/web_profile_info`` embedded posts and the
  ``xdt_shortcode_media`` post query: ``__typename``, ``shortcode``,
  ``display_url``, ``edge_media_to_caption``, ``edge_liked_by``.

``parse_post`` sniffs which shape it got and normalizes both into ``Post``.
"""

from __future__ import annotations

from typing import Any

from .models import Collection, MediaItem, Post, Profile, Reel, User


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _shortcode_to_url(shortcode: str) -> str:
    """Canonical post URL. ``/p/<code>/`` also resolves reels."""
    if not shortcode:
        return ""
    return f"https://www.instagram.com/p/{shortcode}/"


def _largest_candidate(image_versions2: Any) -> dict[str, Any] | None:
    """Pick the highest-resolution entry from an ``image_versions2`` block."""
    candidates = (image_versions2 or {}).get("candidates") or []
    best: dict[str, Any] | None = None
    best_area = -1
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        area = (cand.get("width") or 0) * (cand.get("height") or 0)
        if area > best_area:
            best, best_area = cand, area
    return best


def _caption_text(node: dict[str, Any]) -> str | None:
    """Caption text from either the private-API or GraphQL shape."""
    cap = node.get("caption")
    if isinstance(cap, dict):
        return cap.get("text")
    if isinstance(cap, str):
        return cap
    edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
    if edges:
        first = (edges[0] or {}).get("node") or {}
        return first.get("text")
    return None


def parse_user(node: dict[str, Any] | None) -> User | None:
    """Build a ``User`` from a private-API ``user`` or GraphQL ``owner`` dict."""
    if not node:
        return None
    pk = str(node.get("pk") or node.get("id") or "")
    return User(
        pk=pk,
        username=node.get("username") or "",
        full_name=node.get("full_name"),
        is_verified=bool(node.get("is_verified")),
        is_private=bool(node.get("is_private")),
        profile_pic_url=node.get("profile_pic_url"),
    )


# --------------------------------------------------------------------------
# media leaves
# --------------------------------------------------------------------------
def _media_items_private(node: dict[str, Any]) -> tuple[MediaItem, ...]:
    """Flatten a private-API media node into leaf images/videos."""
    if node.get("media_type") == 8:
        leaves: list[MediaItem] = []
        for child in node.get("carousel_media") or []:
            if isinstance(child, dict):
                leaves.extend(_media_items_private(child))
        return tuple(leaves)

    best_img = _largest_candidate(node.get("image_versions2"))
    image_url = best_img.get("url") if best_img else None
    width = best_img.get("width") if best_img else None
    height = best_img.get("height") if best_img else None

    video_url = None
    videos = node.get("video_versions") or []
    if videos and isinstance(videos[0], dict):
        video_url = videos[0].get("url")
        if width is None:
            width = videos[0].get("width")
        if height is None:
            height = videos[0].get("height")

    media_type = 2 if video_url else (node.get("media_type") or 1)
    return (
        MediaItem(
            media_type=media_type,
            image_url=image_url,
            video_url=video_url,
            width=width,
            height=height,
        ),
    )


def _media_items_graphql(node: dict[str, Any]) -> tuple[MediaItem, ...]:
    """Flatten a GraphQL media node into leaf images/videos."""
    typename = node.get("__typename") or ""
    if "Sidecar" in typename or "edge_sidecar_to_children" in node:
        leaves: list[MediaItem] = []
        edges = ((node.get("edge_sidecar_to_children") or {}).get("edges")) or []
        for edge in edges:
            child = (edge or {}).get("node") or {}
            if child:
                leaves.extend(_media_items_graphql(child))
        return tuple(leaves)

    is_video = bool(node.get("is_video"))
    dims = node.get("dimensions") or {}
    return (
        MediaItem(
            media_type=2 if is_video else 1,
            image_url=node.get("display_url"),
            video_url=node.get("video_url") if is_video else None,
            width=dims.get("width"),
            height=dims.get("height"),
        ),
    )


# --------------------------------------------------------------------------
# posts
# --------------------------------------------------------------------------
def _post_from_private(node: dict[str, Any]) -> Post:
    pk = str(node.get("pk") or "")
    user = node.get("user") or {}
    user_pk = str(user.get("pk") or user.get("id") or "")
    media_id = node.get("id") or (f"{pk}_{user_pk}" if pk and user_pk else pk)
    shortcode = node.get("code") or ""
    location = node.get("location")
    return Post(
        pk=pk,
        id=str(media_id),
        shortcode=shortcode,
        url=_shortcode_to_url(shortcode),
        media_type=node.get("media_type") or 1,
        caption=_caption_text(node),
        media=_media_items_private(node),
        like_count=node.get("like_count"),
        comment_count=node.get("comment_count"),
        view_count=node.get("view_count") or node.get("play_count"),
        taken_at=node.get("taken_at"),
        product_type=node.get("product_type"),
        location=location.get("name") if isinstance(location, dict) else None,
        author=parse_user(user) or None,
    )


def _post_from_graphql(node: dict[str, Any]) -> Post:
    pk = str(node.get("id") or "")
    owner = node.get("owner") or {}
    owner_pk = str(owner.get("id") or owner.get("pk") or "")
    media_id = f"{pk}_{owner_pk}" if pk and owner_pk else pk
    shortcode = node.get("shortcode") or ""
    typename = node.get("__typename") or ""
    is_sidecar = "Sidecar" in typename or "edge_sidecar_to_children" in node
    media_type = 8 if is_sidecar else (2 if node.get("is_video") else 1)
    likes = (
        node.get("edge_liked_by")
        or node.get("edge_media_preview_like")
        or {}
    ).get("count")
    comments = (
        node.get("edge_media_to_comment")
        or node.get("edge_media_to_parent_comment")
        or {}
    ).get("count")
    location = node.get("location")
    return Post(
        pk=pk,
        id=str(media_id),
        shortcode=shortcode,
        url=_shortcode_to_url(shortcode),
        media_type=media_type,
        caption=_caption_text(node),
        media=_media_items_graphql(node),
        like_count=likes,
        comment_count=comments,
        view_count=node.get("video_view_count"),
        taken_at=node.get("taken_at_timestamp"),
        product_type=node.get("product_type"),
        location=location.get("name") if isinstance(location, dict) else None,
        author=parse_user(owner) or None,
    )


def parse_post(node: dict[str, Any] | None) -> Post | None:
    """Normalize either Instagram media shape into a ``Post``.

    Returns ``None`` for empty input or a node matching neither shape.
    """
    if not node or not isinstance(node, dict):
        return None
    if "media_type" in node or "image_versions2" in node:
        return _post_from_private(node)
    if "__typename" in node or "display_url" in node:
        return _post_from_graphql(node)
    return None


def parse_reel(node: dict[str, Any] | None) -> Reel | None:
    """Build a ``Reel`` from a private-API clip media node."""
    if not node or not isinstance(node, dict):
        return None
    pk = str(node.get("pk") or "")
    user = node.get("user") or {}
    user_pk = str(user.get("pk") or user.get("id") or "")
    media_id = node.get("id") or (f"{pk}_{user_pk}" if pk and user_pk else pk)
    shortcode = node.get("code") or ""
    videos = node.get("video_versions") or []
    video_url = videos[0].get("url") if videos and isinstance(videos[0], dict) else None
    best_img = _largest_candidate(node.get("image_versions2"))
    return Reel(
        pk=pk,
        id=str(media_id),
        shortcode=shortcode,
        url=_shortcode_to_url(shortcode),
        caption=_caption_text(node),
        video_url=video_url,
        thumbnail_url=best_img.get("url") if best_img else None,
        like_count=node.get("like_count"),
        comment_count=node.get("comment_count"),
        play_count=node.get("play_count") or node.get("view_count"),
        taken_at=node.get("taken_at"),
        author=parse_user(user) or None,
    )


def parse_profile(user: dict[str, Any] | None) -> Profile | None:
    """Build a ``Profile`` from a ``web_profile_info`` ``data.user`` dict."""
    if not user or not isinstance(user, dict):
        return None
    bio_links = tuple(
        link.get("url")
        for link in (user.get("bio_links") or [])
        if isinstance(link, dict) and link.get("url")
    )
    return Profile(
        pk=str(user.get("id") or user.get("pk") or ""),
        username=user.get("username") or "",
        full_name=user.get("full_name"),
        biography=user.get("biography"),
        external_url=user.get("external_url") or None,
        bio_links=bio_links,
        follower_count=(user.get("edge_followed_by") or {}).get("count"),
        following_count=(user.get("edge_follow") or {}).get("count"),
        post_count=(user.get("edge_owner_to_timeline_media") or {}).get("count"),
        is_private=bool(user.get("is_private")),
        is_verified=bool(user.get("is_verified")),
        is_business=bool(user.get("is_business_account")),
        profile_pic_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
    )


def parse_collection(node: dict[str, Any] | None) -> Collection | None:
    """Build a ``Collection`` from a ``collections/list`` item."""
    if not node or not isinstance(node, dict):
        return None
    collection_id = str(node.get("collection_id") or "")
    if not collection_id:
        return None
    cover = node.get("cover_media")
    cover_url = None
    if isinstance(cover, dict):
        best = _largest_candidate(cover.get("image_versions2"))
        cover_url = best.get("url") if best else None
    return Collection(
        collection_id=collection_id,
        name=node.get("collection_name") or "",
        media_count=node.get("collection_media_count"),
        cover_image_url=cover_url,
    )


# --------------------------------------------------------------------------
# payload-level extractors (consumed by scraper)
# --------------------------------------------------------------------------
def _iter_media_nodes(items: Any) -> list[dict[str, Any]]:
    """Yield the media dict from each feed item, tolerating ``item['media']``.

    Different endpoints either wrap the media under ``item['media']`` or make
    the item itself the media object — handle both.
    """
    nodes: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        media = item.get("media")
        nodes.append(media if isinstance(media, dict) else item)
    return nodes


def extract_home(payload: dict[str, Any]) -> list[Post]:
    """``feed/timeline`` → posts. Skips ads and non-media feed items."""
    out: list[Post] = []
    for item in (payload or {}).get("feed_items") or []:
        if not isinstance(item, dict):
            continue
        media = item.get("media_or_ad")
        if not isinstance(media, dict) or media.get("injected"):
            continue
        post = parse_post(media)
        if post is not None:
            out.append(post)
    return out


def extract_profile_posts(payload: dict[str, Any]) -> list[Post]:
    """Posts from either ``web_profile_info`` (embedded) or ``feed/user``."""
    payload = payload or {}
    user = (payload.get("data") or {}).get("user")
    if isinstance(user, dict):
        out: list[Post] = []
        edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
        for edge in edges:
            post = parse_post((edge or {}).get("node") or {})
            if post is not None:
                out.append(post)
        return out

    out = []
    for node in _iter_media_nodes(payload.get("items")):
        post = parse_post(node)
        if post is not None:
            out.append(post)
    return out


def extract_saved(payload: dict[str, Any]) -> list[Post]:
    """``feed/saved/posts`` or ``feed/collection/{id}/posts`` → posts."""
    out: list[Post] = []
    for node in _iter_media_nodes((payload or {}).get("items")):
        post = parse_post(node)
        if post is not None:
            out.append(post)
    return out


def extract_reels(payload: dict[str, Any]) -> list[Reel]:
    """``clips/user`` → reels."""
    out: list[Reel] = []
    for node in _iter_media_nodes((payload or {}).get("items")):
        reel = parse_reel(node)
        if reel is not None:
            out.append(reel)
    return out


def extract_collections(payload: dict[str, Any]) -> list[Collection]:
    """``collections/list`` → collections."""
    out: list[Collection] = []
    for item in (payload or {}).get("items") or []:
        collection = parse_collection(item)
        if collection is not None:
            out.append(collection)
    return out


def extract_profile(payload: dict[str, Any]) -> Profile | None:
    """``users/web_profile_info`` → a single ``Profile``."""
    user = ((payload or {}).get("data") or {}).get("user")
    if not isinstance(user, dict):
        return None
    return parse_profile(user)


def extract_post_detail(payload: dict[str, Any]) -> Post | None:
    """A post-page GraphQL payload → ``Post``.

    Returns ``None`` for any GraphQL response that isn't the post query, so
    ``intercept_single_response`` keeps waiting for the right one.
    """
    data = (payload or {}).get("data") or {}
    node = data.get("xdt_shortcode_media") or data.get("shortcode_media")
    if not isinstance(node, dict):
        return None
    return parse_post(node)
