"""Pure extractors that turn Instagram JSON payloads into our dataclasses.

No Playwright / IO here. Functions accept dicts (or HTML strings), return
dataclasses, ``None``, or lists. Exhaustively unit-testable with captured
fixtures.

Instagram's web app is GraphQL-driven. The list feeds come back as Relay
connections — ``data.<xdt_*_connection>.edges[].node`` — and the post/media
objects inside use the "private API" media shape (integer ``media_type``,
``code``, ``image_versions2``, ``caption.text``, ``like_count``). A single
post page instead embeds its data in the server-rendered HTML; ``find_embedded``
digs it out.
"""

from __future__ import annotations

import html as _htmllib
import json
import re
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
    """Caption text from a media node's ``caption`` object."""
    cap = node.get("caption")
    if isinstance(cap, dict):
        return cap.get("text")
    if isinstance(cap, str):
        return cap
    return None


# Public play count, in order of preference. ``play_count`` is the figure the
# Reels tab shows on each thumbnail (Instagram + Facebook cross-post plays);
# ``ig_play_count`` is the Instagram-only share, present on REST feeds.
# ``view_count`` is still in the schema but Instagram now always sends null.
_PLAY_COUNT_KEYS = ("play_count", "ig_play_count", "view_count", "video_view_count")


def _play_count(node: dict[str, Any]) -> int | None:
    """First integer play/view count on a media node (0 is a valid count)."""
    for key in _PLAY_COUNT_KEYS:
        value = node.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def parse_user(node: dict[str, Any] | None) -> User | None:
    """Build a ``User`` from a media node's ``user`` sub-object."""
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
def _media_items(node: dict[str, Any]) -> tuple[MediaItem, ...]:
    """Flatten a media node into leaf images/videos (carousels expanded)."""
    if node.get("media_type") == 8:
        leaves: list[MediaItem] = []
        for child in node.get("carousel_media") or []:
            if isinstance(child, dict):
                leaves.extend(_media_items(child))
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


# --------------------------------------------------------------------------
# objects
# --------------------------------------------------------------------------
def parse_post(node: dict[str, Any] | None) -> Post | None:
    """Normalize an Instagram media node into a ``Post``.

    Returns ``None`` for empty input or a node that isn't a media object.
    """
    if not node or not isinstance(node, dict):
        return None
    if not any(k in node for k in ("media_type", "image_versions2", "code")):
        return None

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
        media=_media_items(node),
        like_count=node.get("like_count"),
        comment_count=node.get("comment_count"),
        view_count=_play_count(node),
        taken_at=node.get("taken_at"),
        product_type=node.get("product_type"),
        location=location.get("name") if isinstance(location, dict) else None,
        author=parse_user(user) or None,
    )


def parse_reel(node: dict[str, Any] | None) -> Reel | None:
    """Build a ``Reel`` from a clip media node."""
    if not node or not isinstance(node, dict):
        return None
    if not any(k in node for k in ("media_type", "video_versions", "code")):
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
        play_count=_play_count(node),
        taken_at=node.get("taken_at"),
        author=parse_user(user) or None,
    )


def parse_profile(user: dict[str, Any] | None) -> Profile | None:
    """Build a ``Profile`` from a ``PolarisProfilePageContentQuery`` user."""
    if not user or not isinstance(user, dict):
        return None
    bio_links = tuple(
        (link.get("url") or link.get("lynx_url"))
        for link in (user.get("bio_links") or [])
        if isinstance(link, dict) and (link.get("url") or link.get("lynx_url"))
    )
    hd = user.get("hd_profile_pic_url_info")
    hd_url = hd.get("url") if isinstance(hd, dict) else None
    return Profile(
        pk=str(user.get("pk") or user.get("id") or ""),
        username=user.get("username") or "",
        full_name=user.get("full_name"),
        biography=user.get("biography"),
        external_url=user.get("external_url") or None,
        bio_links=bio_links,
        follower_count=user.get("follower_count"),
        following_count=user.get("following_count"),
        post_count=user.get("media_count"),
        is_private=bool(user.get("is_private")),
        is_verified=bool(user.get("is_verified")),
        is_business=bool(user.get("is_business")),
        profile_pic_url=hd_url or user.get("profile_pic_url"),
    )


def _cover_url(node: dict[str, Any]) -> str | None:
    cover = node.get("cover_media")
    if isinstance(cover, dict):
        best = _largest_candidate(cover.get("image_versions2"))
        if best:
            return best.get("url")
    # "All posts" carries no cover_media; its thumbnails are in cover_media_list.
    for item in node.get("cover_media_list") or []:
        if isinstance(item, dict):
            best = _largest_candidate(item.get("image_versions2"))
            if best:
                return best.get("url")
    return None


def parse_collection(node: dict[str, Any] | None) -> Collection | None:
    """Build a ``Collection`` from a collections-connection node."""
    if not node or not isinstance(node, dict):
        return None
    collection_id = str(node.get("collection_id") or "")
    if not collection_id:
        return None
    return Collection(
        collection_id=collection_id,
        name=node.get("collection_name") or "",
        media_count=node.get("collection_media_count"),
        cover_image_url=_cover_url(node),
    )


# --------------------------------------------------------------------------
# connection- and payload-level extractors (consumed by endpoints)
# --------------------------------------------------------------------------
def _edges(connection: Any) -> list[dict[str, Any]]:
    """``edges`` of a Relay connection, dicts only."""
    if not isinstance(connection, dict):
        return []
    return [e for e in connection.get("edges") or [] if isinstance(e, dict)]


def _connection_edges(payload: Any, connection_key: str) -> list[dict[str, Any]]:
    """Return ``data.<connection_key>.edges`` from a GraphQL payload."""
    data = (payload or {}).get("data") or {}
    return _edges(data.get(connection_key))


def connection_cursor(connection: Any) -> str | None:
    """``page_info.end_cursor`` when there is a next page, else ``None``."""
    if not isinstance(connection, dict):
        return None
    info = connection.get("page_info") or {}
    if not info.get("has_next_page"):
        return None
    cursor = info.get("end_cursor")
    return str(cursor) if cursor else None


def rest_cursor(payload: Any, *, next_key: str = "next_max_id",
                more_key: str = "more_available") -> str | None:
    """REST ``next_max_id`` when the feed says there is more, else ``None``."""
    if not isinstance(payload, dict) or not payload.get(more_key):
        return None
    cursor = payload.get(next_key)
    return str(cursor) if cursor not in (None, "") else None


def _iter_media_nodes(items: Any) -> list[dict[str, Any]]:
    """Yield the media dict from each REST feed item (``item['media']`` or item)."""
    nodes: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        media = item.get("media")
        nodes.append(media if isinstance(media, dict) else item)
    return nodes


_TIMELINE_CONNECTION_KEY = "xdt_api__v1__feed__timeline__connection"
TIMELINE_CONNECTION_KEY = _TIMELINE_CONNECTION_KEY


def home_posts(connection: Any) -> list[Post]:
    """Timeline connection → posts. Ads, suggestions and injected items skipped."""
    out: list[Post] = []
    for edge in _edges(connection):
        node = edge.get("node") or {}
        if node.get("ad") is not None or node.get("explore_story") is not None:
            continue
        media = node.get("media")
        if not isinstance(media, dict) or media.get("injected"):
            continue
        post = parse_post(media)
        if post is not None:
            out.append(post)
    return out


def extract_home(payload: dict[str, Any]) -> list[Post]:
    """Home-feed connection payload → posts. Ads/netego nodes skipped."""
    data = (payload or {}).get("data") or {}
    return home_posts(data.get(_TIMELINE_CONNECTION_KEY))


def extract_home_html(html: str) -> list[Post]:
    """Posts from the home feed's first page, embedded in the ``/`` page HTML.

    Instagram server-renders the first batch of the timeline; subsequent pages
    arrive over the pagination query. This seeds those first posts.
    """
    connection = find_embedded(html, _TIMELINE_CONNECTION_KEY)
    if not isinstance(connection, dict):
        return []
    return extract_home({"data": {_TIMELINE_CONNECTION_KEY: connection}})


def connection_posts(connection: Any) -> list[Post]:
    """A connection whose edge nodes are media (grid, collection) → posts."""
    out: list[Post] = []
    for edge in _edges(connection):
        post = parse_post(edge.get("node") or {})
        if post is not None:
            out.append(post)
    return out


def extract_profile_posts(payload: dict[str, Any]) -> list[Post]:
    """``PolarisProfilePostsQuery`` → posts (each edge node is the media)."""
    data = (payload or {}).get("data") or {}
    return connection_posts(data.get("xdt_api__v1__feed__user_timeline_graphql_connection"))


def extract_collection_media(payload: dict[str, Any]) -> list[Post]:
    """``PolarisSavedCollectionPageWWWQuery`` → posts.

    These nodes carry no ``video_versions`` nor ``taken_at``.
    """
    data = (payload or {}).get("data") or {}
    collection = data.get("fetch__MediaCollection")
    if not isinstance(collection, dict):
        return []
    return connection_posts(collection.get("media"))


def _reels_edges(payload: Any) -> list[dict[str, Any]]:
    """Edges of a Reels-tab payload, in either of the shapes Instagram served.

    Current: ``data.fetch__XDTUserDict.clips_connection.edges``.
    Legacy: ``data.xdt_api__v1__clips__user__connection_v2.edges``.
    """
    data = (payload or {}).get("data") or {}
    user = data.get("fetch__XDTUserDict")
    if isinstance(user, dict):
        connection = user.get("clips_connection") or {}
        edges = connection.get("edges") or []
        return [e for e in edges if isinstance(e, dict)]
    return _connection_edges(payload, "xdt_api__v1__clips__user__connection_v2")


def _reels_from_edges(edges: list[dict[str, Any]]) -> list[Reel]:
    out: list[Reel] = []
    for edge in edges:
        node = edge.get("node") or {}
        media = node.get("media")
        reel = parse_reel(media if isinstance(media, dict) else node)
        if reel is not None:
            out.append(reel)
    return out


def connection_reels(connection: Any) -> list[Reel]:
    """A clips connection → reels."""
    return _reels_from_edges(_edges(connection))


def extract_reels(payload: dict[str, Any]) -> list[Reel]:
    """``PolarisProfileReelsTabContentQuery`` (and ``_connection``) → reels."""
    return _reels_from_edges(_reels_edges(payload))


# Auto-collections that are not lists of posts.
_NON_POST_COLLECTION_IDS = frozenset({"AUDIO_AUTO_COLLECTION", "PLAYLIST_AUTO_COLLECTION"})
_NON_POST_COLLECTION_TYPES = frozenset({"XDTAudioAutoCollection"})


def connection_collections(connection: Any) -> list[Collection]:
    """Save-collections connection → collections (audio/playlist dropped)."""
    out: list[Collection] = []
    for edge in _edges(connection):
        node = edge.get("node") or {}
        if node.get("__typename") in _NON_POST_COLLECTION_TYPES:
            continue
        if node.get("collection_id") in _NON_POST_COLLECTION_IDS:
            continue
        collection = parse_collection(node)
        if collection is not None:
            out.append(collection)
    return out


def extract_collections(payload: dict[str, Any]) -> list[Collection]:
    """``PolarisProfileSavedTabContentQuery`` → save-collections.

    Current root: ``data.viewer.collections_unified_with_auto_collections``;
    the older ``xdt_api__v1__collections__list_graphql_connection`` is still
    accepted.
    """
    data = (payload or {}).get("data") or {}
    viewer = data.get("viewer")
    if isinstance(viewer, dict) and isinstance(
        viewer.get("collections_unified_with_auto_collections"), dict
    ):
        return connection_collections(viewer["collections_unified_with_auto_collections"])
    return connection_collections(data.get("xdt_api__v1__collections__list_graphql_connection"))


def search_users(connection: Any) -> list[User]:
    """Search-box result → users (``is_private`` is not in this payload)."""
    out: list[User] = []
    if not isinstance(connection, dict):
        return out
    for row in connection.get("users") or []:
        user = parse_user((row or {}).get("user") if isinstance(row, dict) else None)
        if user is not None:
            out.append(user)
    return out


def extract_search_users(payload: dict[str, Any]) -> list[User]:
    """``PolarisSearchBoxRefetchableQuery`` → users."""
    data = (payload or {}).get("data") or {}
    return search_users(data.get("xdt_api__v1__fbsearch__topsearch_connection"))


def extract_saved(payload: dict[str, Any]) -> list[Post]:
    """REST ``feed/saved/posts`` or ``feed/collection/.../posts`` → posts."""
    out: list[Post] = []
    for node in _iter_media_nodes((payload or {}).get("items")):
        post = parse_post(node)
        if post is not None:
            out.append(post)
    return out


def extract_profile(payload: dict[str, Any]) -> Profile | None:
    """``PolarisProfilePageContentQuery`` → a single ``Profile``.

    Returns ``None`` for any other GraphQL response, so a single-response
    interceptor keeps waiting for the right one.
    """
    user = ((payload or {}).get("data") or {}).get("user")
    if not isinstance(user, dict):
        return None
    return parse_profile(user)


# --------------------------------------------------------------------------
# HTML-embedded data (single post page)
# --------------------------------------------------------------------------
_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S
)


def _deep_find(obj: Any, key: str) -> Any | None:
    """Depth-first search for the first value stored under ``key``."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _deep_find(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _deep_find(value, key)
            if found is not None:
                return found
    return None


def find_embedded(html: str, key: str) -> Any | None:
    """Find a value stored under ``key`` in any embedded JSON ``<script>`` blob.

    Instagram server-renders data into ``<script type="application/json">``
    tags; this digs out the first occurrence of ``key`` anywhere inside them.
    """
    for raw in _JSON_SCRIPT_RE.findall(html or ""):
        for candidate in (raw, _htmllib.unescape(raw)):
            try:
                obj = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            found = _deep_find(obj, key)
            if found is not None:
                return found
            break
    return None
