"""FastMCP server exposing read-only Instagram tools to Claude.

Run with::

    venv/bin/python -m instagram_sdk.server

Register it with Claude Code at user scope so it's available from every
project (see README "Connect Claude"). The session file is the Playwright
storage_state JSON written by ``scripts/auth_login.py``; the server discovers
it from ``INSTAGRAM_SESSION_FILE``, the ``INSTAGRAM_SESSION_DIR`` directory,
or — for backward compatibility — the legacy in-repo ``sessions/`` folder.

All tools are read-only. Every Instagram request is an in-page ``fetch`` from
one warm, resource-blocked tab, serialized behind a lock and paced with
randomized pauses — keep ``limit`` values modest; every ``limit`` is capped at
``MAX_LIMIT``. If Instagram pushes back (rate limit, challenge, checkpoint)
the call fails with an explicit error and the account is held: nothing
retries on its own.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

# Absolute imports (not ``from . import ...``) so this module can also be
# loaded as a single file by ``mcp dev src/instagram_sdk/server.py`` — that
# loader uses ``spec_from_file_location`` and the module has no
# ``__package__``, which breaks relative imports.
from instagram_sdk import downloader
from instagram_sdk.accounts import Account
from instagram_sdk.auth import session_summary
from instagram_sdk.cursors import MAX_PAGES_PER_CALL
from instagram_sdk.endpoints import home, media, post, profile, saved
from instagram_sdk.models import (
    DownloadedMedia,
    MediaDownloadResult,
    MediaItem,
    SkippedMedia,
)
from instagram_sdk.runtime import Runtime
from instagram_sdk.settings import Settings

load_dotenv()

mcp = FastMCP("instagram-mcp")

# Hard cap on every ``limit``: more posts means more requests on the account.
MAX_LIMIT = 50


def _resolve_session_path() -> Path:
    """The session file, looked up on every call (a login needs no restart)."""
    return Settings.from_env().resolve_session_path()


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """The process runtime (browser, lock, state), created on first use."""
    global _runtime
    if _runtime is None:
        _runtime = Runtime(
            Settings.from_env(),
            # Late-bound so tests can patch ``_resolve_session_path``.
            session_source=lambda: _resolve_session_path(),
        )
    return _runtime


def _clamp_limit(limit: int) -> int:
    """Keep a tool's ``limit`` between 1 and ``MAX_LIMIT``."""
    return max(1, min(limit, MAX_LIMIT))


def _serialize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def _select_indices(
    items: tuple[MediaItem, ...],
    indices: list[int] | None,
) -> list[tuple[int, MediaItem]]:
    """Pair each media item with its original 0-based index.

    When ``indices`` is given, keep only those positions. The original index is
    retained so saved filenames stay stable regardless of the filter.
    """
    enumerated = list(enumerate(items))
    if indices is None:
        return enumerated
    wanted = set(indices)
    return [(i, item) for i, item in enumerated if i in wanted]


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a ``.part`` file + rename, so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@mcp.tool()
async def auth_status() -> dict[str, Any]:
    """Report whether an Instagram session is loaded and likely valid.

    Returns the session file path, the handle inferred from the filename,
    last-modified timestamp (proxy for last login), and an `authenticated`
    flag based on the presence of `sessionid` + `ds_user_id` cookies.

    Also reports ``state`` (``ok`` | ``blocked`` | ``cooldown`` | ``expired``)
    and ``block`` (kind, detail, until) if Instagram stopped the account
    earlier: a held account fails fast, without network, until it clears.

    This does NOT make a network request — to verify the session actually
    works, call any other tool. Session-expired errors there point back here.
    """
    path = _resolve_session_path()
    summary = session_summary(path)
    summary.update(get_runtime().state.summary(Account.from_session(path)))
    return summary


@mcp.tool()
async def get_saved_posts(
    collection_id: str | None = None,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Read your saved posts (private to your account).

    Args:
        collection_id: Restrict to one save-collection (its id, from
            `get_saved_collections`). Omit for the full "All Posts" bucket.
        limit: Max posts to return (default 24, capped at 50). Keep this modest.
    """
    page = await get_runtime().run(
        None,
        lambda ig: saved.fetch_saved_page(
            ig,
            collection_id=collection_id,
            limit=_clamp_limit(limit),
            max_pages=MAX_PAGES_PER_CALL,
        ),
    )
    return _serialize(page.items)


@mcp.tool()
async def get_saved_collections() -> list[dict[str, Any]]:
    """List your save-collections — each with its name and item count.

    The implicit "All Posts" bucket is included. Pass a collection's id to
    `get_saved_posts` to read just that collection.
    """
    collections = await get_runtime().run(None, lambda ig: saved.fetch_collections(ig))
    return _serialize(collections)


@mcp.tool()
async def get_home_feed(limit: int = 15) -> list[dict[str, Any]]:
    """Read your home feed (timeline). Sponsored posts are dropped.

    Args:
        limit: Max posts to return (default 15, capped at 50). Keep this modest.
    """
    page = await get_runtime().run(
        None,
        lambda ig: home.fetch_home_page(
            ig, limit=_clamp_limit(limit), max_pages=MAX_PAGES_PER_CALL
        ),
    )
    return _serialize(page.items)


@mcp.tool()
async def get_user_profile(username: str) -> dict[str, Any] | None:
    """Fetch a user's public profile.

    Returns bio, counters (posts, followers, following), private/verified
    flags, profile picture, external URL and bio links.

    Args:
        username: Instagram username without the leading "@".
    """
    result = await get_runtime().run(
        None, lambda ig: profile.fetch_profile(ig, username=username)
    )
    return _serialize(result)


@mcp.tool()
async def get_user_posts(
    username: str,
    limit: int = 24,
    include_reels: bool = False,
) -> list[dict[str, Any]]:
    """Fetch posts from a user's profile grid.

    Args:
        username: Instagram username without the leading "@".
        limit: Max posts to return (default 24, capped at 50).
        include_reels: Keep reels in the result (excluded by default — use
            `get_user_reels` for a reels-only view).
    """
    page = await get_runtime().run(
        None,
        lambda ig: profile.fetch_posts_page(
            ig,
            username=username,
            limit=_clamp_limit(limit),
            include_reels=include_reels,
            max_pages=MAX_PAGES_PER_CALL,
        ),
    )
    return _serialize(page.items)


@mcp.tool()
async def get_post(id_or_url: str) -> dict[str, Any] | None:
    """Fetch one post by shortcode or URL.

    Returns the author, caption, media (images / videos / carousel slides) and
    like/comment counts, plus ``view_count`` (plays) for reels.

    Args:
        id_or_url: The post shortcode (e.g. "C8xY1zAbCdE") or a full
            https://www.instagram.com/p/<shortcode>/ URL. Reel URLs work too.
    """
    result = await get_runtime().run(
        None, lambda ig: post.fetch(ig, id_or_url=id_or_url)
    )
    return _serialize(result)


@mcp.tool()
async def get_user_reels(
    username: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Fetch reels from a user's Reels tab.

    Args:
        username: Instagram username without the leading "@".
        limit: Max reels to return (default 12, capped at 50).
    """
    page = await get_runtime().run(
        None,
        lambda ig: profile.fetch_reels_page(
            ig, username=username, limit=_clamp_limit(limit), max_pages=MAX_PAGES_PER_CALL
        ),
    )
    return _serialize(page.items)


@mcp.tool(structured_output=False)
async def download_media(
    id_or_url: str,
    indices: list[int] | None = None,
    download_videos: bool = False,
) -> list[Any]:
    """Download a post's media so Claude can actually see it.

    Every other tool returns JSON only — for a post with a photo or a reel you
    get the URL and metadata, but never the pixels. This tool fetches the bytes:

    - **Images** are downloaded, saved under the downloads directory, AND
      returned inline so they enter the conversation as viewable images.
    - **Videos / reels** are NOT fetched unless ``download_videos=True`` — they
      can be large and Claude cannot watch them anyway. Otherwise they appear
      in the result's ``skipped`` list with their URL.

    The post is re-fetched fresh first: Instagram CDN URLs are signed and
    short-lived, so this resolves the URLs and downloads them in one session.
    A carousel's slides are each downloaded; pick specific ones with ``indices``.

    Args:
        id_or_url: A post shortcode (e.g. "C8xY1zAbCdE") or a full
            https://www.instagram.com/p/<shortcode>/ URL. Reel URLs work too.
        indices: 0-based positions of the media items to download. None = all
            (relevant for carousels; a single post/reel has one item).
        download_videos: Opt in to downloading videos/reels. When set there is
            no size cap — the opt-in is the only gate.

    Returns:
        A list of inline images followed by one JSON summary:
        ``{shortcode, downloaded:[...], skipped:[...]}``. Saved files are named
        ``post_<shortcode>_<index>.<ext>`` under the downloads directory
        (``~/.instagram-mcp/downloads/`` by default, or
        ``INSTAGRAM_DOWNLOADS_DIR``).
    """
    runtime = get_runtime()
    shortcode, items = await runtime.run(
        None, lambda ig: media.resolve_post_media(ig, id_or_url=id_or_url)
    )

    selected = _select_indices(items, indices)

    downloads_dir = Settings.from_env().downloads_dir
    downloads_dir.mkdir(parents=True, exist_ok=True)
    client = runtime.http_client()

    images: list[Image] = []
    downloaded: list[DownloadedMedia] = []
    skipped: list[SkippedMedia] = []

    for index, item in selected:
        is_video = item.media_type == 2
        kind = "video" if is_video else "image"
        url = item.video_url if is_video else item.image_url

        # Videos/reels are opt-in only — never auto-downloaded.
        if is_video and not download_videos:
            skipped.append(
                SkippedMedia(
                    kind=kind,
                    source_url=url or "",
                    reason="video_not_requested",
                )
            )
            continue

        if not url:
            skipped.append(
                SkippedMedia(kind=kind, source_url="", reason="no_url")
            )
            continue

        # Images get a safety-net cap; opted-in videos get none (user's call).
        max_bytes = None if is_video else downloader.IMAGE_MAX_BYTES
        # CDN downloads run outside the navigation lock, two at most.
        async with runtime.download_semaphore():
            outcome = await downloader.fetch_bytes(client, url, max_bytes=max_bytes)

        if not outcome.ok or outcome.data is None:
            skipped.append(
                SkippedMedia(
                    kind=kind,
                    source_url=url,
                    reason=outcome.reason or downloader.REASON_DOWNLOAD_ERROR,
                )
            )
            continue

        fmt = downloader.content_type_to_format(
            outcome.content_type, downloader.extension_from_url(url)
        )
        ext = downloader.format_to_extension(fmt)
        path = downloads_dir / f"post_{shortcode}_{index:02d}.{ext}"
        _write_atomic(path, outcome.data)

        downloaded.append(
            DownloadedMedia(
                kind=kind,
                source_url=url,
                saved_path=str(path),
                byte_size=len(outcome.data),
                content_type=outcome.content_type,
            )
        )
        # Only images go back inline — Claude can view images, not video bytes.
        if not is_video:
            images.append(Image(data=outcome.data, format=fmt))

    result = MediaDownloadResult(
        shortcode=shortcode,
        downloaded=tuple(downloaded),
        skipped=tuple(skipped),
    )
    return [*images, _serialize(result)]


async def _amain() -> None:
    try:
        await mcp.run_stdio_async()
    finally:
        # Same event loop as the tools: close Chromium and the driver cleanly.
        if _runtime is not None:
            await _runtime.shutdown()


def main() -> None:
    # stdout is the MCP channel in stdio mode: logs go to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.getenv("INSTAGRAM_LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    import anyio

    anyio.run(_amain)


if __name__ == "__main__":
    main()
