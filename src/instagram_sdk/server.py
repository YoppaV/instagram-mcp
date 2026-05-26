"""FastMCP server exposing read-only Instagram tools to Claude.

Run with::

    venv/bin/python -m instagram_sdk.server

Register it with Claude Code at user scope so it's available from every
project (see README "Connect Claude"). The session file is the Playwright
storage_state JSON written by ``scripts/auth_login.py``; the server discovers
it from ``INSTAGRAM_SESSION_FILE``, the ``INSTAGRAM_SESSION_DIR`` directory,
or — for backward compatibility — the legacy in-repo ``sessions/`` folder.

All tools are read-only. Instagram detects automation aggressively, so calls
are serialized through one browser and paced with randomized pauses — keep
``limit`` values modest.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

# Absolute imports (not ``from . import ...``) so this module can also be
# loaded as a single file by ``mcp dev src/instagram_sdk/server.py`` — that
# loader uses ``spec_from_file_location`` and the module has no
# ``__package__``, which breaks relative imports.
from instagram_sdk import downloader
from instagram_sdk.auth import (
    USER_AGENT,
    SessionExpiredError,
    handle_from_session_path,
    session_summary,
)
from instagram_sdk.browser import DEFAULT_IDLE_TIMEOUT_S, BrowserSession
from instagram_sdk.endpoints import home, media, post, profile, saved
from instagram_sdk.models import (
    DownloadedMedia,
    MediaDownloadResult,
    MediaItem,
    SkippedMedia,
)

load_dotenv()

mcp = FastMCP("instagram-mcp")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_XDG_SESSION_DIR = Path.home() / ".instagram-mcp" / "sessions"
_LEGACY_SESSION_DIR = _PROJECT_ROOT / "sessions"
_SESSION_SUFFIX = "_instagram_state"


def _candidate_session_dirs() -> list[Path]:
    """Where to look for ``<handle>_instagram_state.json`` files, in order.

    Sessions live outside the repo by default (``~/.instagram-mcp/sessions/``)
    so a public checkout never accidentally tracks logged-in cookies.
    ``INSTAGRAM_SESSION_DIR`` overrides the primary directory; the legacy
    in-repo ``sessions/`` is checked second so existing local workflows keep
    working without forced migration.
    """
    override = os.getenv("INSTAGRAM_SESSION_DIR", "").strip()
    if override:
        return [Path(override).expanduser()]
    return [_XDG_SESSION_DIR, _LEGACY_SESSION_DIR]


def _resolve_downloads_dir() -> Path:
    """Where ``download_media`` writes fetched media.

    Defaults to ``~/.instagram-mcp/downloads/`` so a pip-installed package
    doesn't write into site-packages and a checkout doesn't pollute the
    repo. ``INSTAGRAM_DOWNLOADS_DIR`` overrides it — the ``social-ingest``
    pipeline points this straight at the vault's ``media/`` folder so
    downloads land where the capture note expects.
    """
    override = os.getenv("INSTAGRAM_DOWNLOADS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".instagram-mcp" / "downloads"


_DOWNLOADS_DIR = _resolve_downloads_dir()


def _resolve_session_path() -> Path:
    explicit = os.getenv("INSTAGRAM_SESSION_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    handle = os.getenv("INSTAGRAM_USERNAME", "").strip()
    candidates = _candidate_session_dirs()

    if handle:
        filename = f"{handle}_instagram_state.json"
        for directory in candidates:
            candidate = directory / filename
            if candidate.exists():
                return candidate
        # No file found yet — point at the primary location for a clear error.
        return candidates[0] / filename

    for directory in candidates:
        matches = sorted(directory.glob("*_instagram_state.json"))
        if matches:
            return matches[0]

    return candidates[0] / "instagram_state.json"


def _idle_timeout() -> int:
    raw = os.getenv("BROWSER_IDLE_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_IDLE_TIMEOUT_S
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_S


def _headless() -> bool:
    raw = os.getenv("BROWSER_HEADLESS", "true").strip().lower()
    return raw not in ("0", "false", "no")


_session_path = _resolve_session_path()
_browser = BrowserSession(
    _session_path,
    idle_timeout_s=_idle_timeout(),
    headless=_headless(),
)


def _resolve_own_handle() -> str:
    """The authenticated account's handle, derived from the session filename.

    Saved posts and collections are private, so those tools must navigate
    under ``/<handle>/saved/...`` — and ``<handle>`` is whoever logged in.
    """
    stem = _session_path.stem
    if not stem.endswith(_SESSION_SUFFIX) or len(stem) <= len(_SESSION_SUFFIX):
        raise SessionExpiredError(
            f"Cannot determine your Instagram handle from session file "
            f"{_session_path.name!r}. Name it '<handle>_instagram_state.json' "
            "— re-run: python -m scripts.auth_login"
        )
    return handle_from_session_path(_session_path)


def _serialize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Lazily build the shared AsyncClient used for media downloads.

    Created on first use so merely importing this module (e.g. in tests) never
    opens a client. Reused across calls — httpx pools connections.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": USER_AGENT},
        )
    return _http_client


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


@mcp.tool()
async def auth_status() -> dict[str, Any]:
    """Report whether an Instagram session is loaded and likely valid.

    Returns the session file path, the handle inferred from the filename,
    last-modified timestamp (proxy for last login), and an `authenticated`
    flag based on the presence of `sessionid` + `ds_user_id` cookies.

    This does NOT make a network request — to verify the session actually
    works, call any other tool. Session-expired errors there point back here.
    """
    return session_summary(_session_path)


@mcp.tool()
async def get_saved_posts(
    collection_id: str | None = None,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Read your saved posts (private to your account).

    Args:
        collection_id: Restrict to one save-collection (its id, from
            `get_saved_collections`). Omit for the full "All Posts" bucket.
        limit: Max posts to return (default 24). Keep this modest.
    """
    handle = _resolve_own_handle()
    async with _browser.page() as page:
        posts = await saved.fetch_saved(
            page, handle=handle, collection_id=collection_id, limit=limit
        )
    return _serialize(posts)


@mcp.tool()
async def get_saved_collections() -> list[dict[str, Any]]:
    """List your save-collections — each with its name and item count.

    The implicit "All Posts" bucket is included. Pass a collection's id to
    `get_saved_posts` to read just that collection.
    """
    handle = _resolve_own_handle()
    async with _browser.page() as page:
        collections = await saved.fetch_collections(page, handle=handle)
    return _serialize(collections)


@mcp.tool()
async def get_home_feed(limit: int = 15) -> list[dict[str, Any]]:
    """Read your home feed (timeline). Sponsored posts are dropped.

    Args:
        limit: Max posts to return (default 15). Keep this modest.
    """
    async with _browser.page() as page:
        posts = await home.fetch(page, limit=limit)
    return _serialize(posts)


@mcp.tool()
async def get_user_profile(username: str) -> dict[str, Any] | None:
    """Fetch a user's public profile.

    Returns bio, counters (posts, followers, following), private/verified
    flags, profile picture, external URL and bio links.

    Args:
        username: Instagram username without the leading "@".
    """
    async with _browser.page() as page:
        result = await profile.fetch_profile(page, username=username)
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
        limit: Max posts to return (default 24).
        include_reels: Keep reels in the result (excluded by default — use
            `get_user_reels` for a reels-only view).
    """
    async with _browser.page() as page:
        posts = await profile.fetch_posts(
            page, username=username, limit=limit, include_reels=include_reels
        )
    return _serialize(posts)


@mcp.tool()
async def get_post(id_or_url: str) -> dict[str, Any] | None:
    """Fetch one post by shortcode or URL.

    Returns the author, caption, media (images / videos / carousel slides) and
    like/comment counts.

    Args:
        id_or_url: The post shortcode (e.g. "C8xY1zAbCdE") or a full
            https://www.instagram.com/p/<shortcode>/ URL. Reel URLs work too.
    """
    async with _browser.page() as page:
        result = await post.fetch(page, id_or_url=id_or_url)
    return _serialize(result)


@mcp.tool()
async def get_user_reels(
    username: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Fetch reels from a user's Reels tab.

    Args:
        username: Instagram username without the leading "@".
        limit: Max reels to return (default 12).
    """
    async with _browser.page() as page:
        reels = await profile.fetch_reels(page, username=username, limit=limit)
    return _serialize(reels)


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
    async with _browser.page() as page:
        shortcode, items = await media.resolve_post_media(
            page, id_or_url=id_or_url
        )

    selected = _select_indices(items, indices)

    _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    client = _get_http_client()

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
        path = _DOWNLOADS_DIR / f"post_{shortcode}_{index:02d}.{ext}"
        path.write_bytes(outcome.data)

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


def main() -> None:
    try:
        mcp.run(transport="stdio")
    except SessionExpiredError as exc:
        # Surface a clean message; the MCP layer otherwise hides exceptions
        # in stdio mode.
        import sys

        print(f"instagram-sdk: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
