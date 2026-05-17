"""FastMCP server exposing read-only Instagram tools to Claude.

Run with::

    venv/bin/python -m instagram_sdk.server

Configure clients (Claude Code: ``.mcp.json`` at repo root). The session file
is the Playwright storage_state JSON written by ``scripts/auth_login.py``;
the server discovers it from ``INSTAGRAM_SESSION_FILE`` or the ``sessions/``
directory.

All tools are read-only. Instagram detects automation aggressively, so calls
are serialized through one browser and paced with randomized pauses — keep
``limit`` values modest.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .auth import SessionExpiredError, handle_from_session_path, session_summary
from .browser import DEFAULT_IDLE_TIMEOUT_S, BrowserSession
from .endpoints import home, post, profile, saved

load_dotenv()

mcp = FastMCP("instagram-mcp")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_SESSION_DIR = _PROJECT_ROOT / "sessions"
_SESSION_SUFFIX = "_instagram_state"


def _resolve_session_path() -> Path:
    explicit = os.getenv("INSTAGRAM_SESSION_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    handle = os.getenv("INSTAGRAM_USERNAME", "").strip()
    if handle:
        return _DEFAULT_SESSION_DIR / f"{handle}_instagram_state.json"

    candidates = sorted(_DEFAULT_SESSION_DIR.glob("*_instagram_state.json"))
    if candidates:
        return candidates[0]

    return _DEFAULT_SESSION_DIR / "instagram_state.json"


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
