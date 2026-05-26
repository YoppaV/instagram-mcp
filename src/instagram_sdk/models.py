"""Immutable dataclasses representing Instagram resources.

These dataclasses are the public shape that MCP tools serialize and return.
They are intentionally Instagram-flavoured but framework-free — no Playwright,
no MCP imports.

``media_type`` follows Instagram's own integer encoding:
``1`` = image, ``2`` = video, ``8`` = carousel (multiple children).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaItem:
    """One image or video — a single leaf of a post.

    A carousel post is represented by a ``Post`` whose ``media`` tuple holds
    one ``MediaItem`` per slide; each slide here is itself a plain image or
    video (``media_type`` 1 or 2).
    """

    media_type: int  # 1 image, 2 video
    image_url: str | None
    video_url: str | None
    width: int | None
    height: int | None


@dataclass(frozen=True)
class User:
    """Lightweight account reference (post author, list member)."""

    pk: str
    username: str
    full_name: str | None
    is_verified: bool
    is_private: bool
    profile_pic_url: str | None


@dataclass(frozen=True)
class Post:
    pk: str
    id: str  # "{pk}_{user_pk}" — stable dedup key across endpoints
    shortcode: str
    url: str
    media_type: int
    caption: str | None
    media: tuple[MediaItem, ...]
    like_count: int | None
    comment_count: int | None
    view_count: int | None  # videos only
    taken_at: int | None  # unix seconds
    product_type: str | None  # "feed" | "carousel_container" | "clips" | "igtv"
    location: str | None
    author: User | None


@dataclass(frozen=True)
class Reel:
    pk: str
    id: str
    shortcode: str
    url: str
    caption: str | None
    video_url: str | None
    thumbnail_url: str | None
    like_count: int | None
    comment_count: int | None
    play_count: int | None
    taken_at: int | None
    author: User | None


@dataclass(frozen=True)
class Profile:
    pk: str
    username: str
    full_name: str | None
    biography: str | None
    external_url: str | None
    bio_links: tuple[str, ...]
    follower_count: int | None
    following_count: int | None
    post_count: int | None
    is_private: bool
    is_verified: bool
    is_business: bool
    profile_pic_url: str | None


@dataclass(frozen=True)
class Collection:
    """A saved-posts collection (or the implicit "All Posts" bucket)."""

    collection_id: str
    name: str
    media_count: int | None
    cover_image_url: str | None


@dataclass(frozen=True)
class DownloadedMedia:
    """A media item the ``download_media`` tool fetched and saved to disk."""

    kind: str  # "image" | "video"
    source_url: str
    saved_path: str
    byte_size: int
    content_type: str


@dataclass(frozen=True)
class SkippedMedia:
    """A media item ``download_media`` did NOT save, with the reason why.

    ``reason`` is one of: ``video_not_requested`` (a video was found but
    ``download_videos`` was False), ``no_url`` (the post carried no usable
    media URL), ``too_large``, ``http_404``, ``download_error``.
    """

    kind: str  # "image" | "video"
    source_url: str
    reason: str


@dataclass(frozen=True)
class MediaDownloadResult:
    """Summary payload returned (alongside inline images) by ``download_media``."""

    shortcode: str
    downloaded: tuple[DownloadedMedia, ...]
    skipped: tuple[SkippedMedia, ...]
