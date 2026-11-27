"""FastMCP server exposing read-only Instagram tools to Claude.

Run with::

    venv/bin/python -m instagram_sdk.server

Register it with Claude Code at user scope so it's available from every
project (see README "Connect Claude"). The session file is the Playwright
storage_state JSON written by ``scripts/auth_login.py``; every
``<handle>_instagram_state.json`` in ``INSTAGRAM_SESSION_DIR`` (or the legacy
in-repo ``sessions/`` folder), plus ``INSTAGRAM_SESSION_FILE``, is one account.
Every tool takes an optional ``account``; without it the default account
is used (``INSTAGRAM_DEFAULT_ACCOUNT``, or the only session there is).

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
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

# Absolute imports (not ``from . import ...``) so this module can also be
# loaded as a single file by ``mcp dev src/instagram_sdk/server.py`` — that
# loader uses ``spec_from_file_location`` and the module has no
# ``__package__``, which breaks relative imports.
from instagram_sdk import downloader
from instagram_sdk.accounts import Account, AccountRegistry, DefaultChoice
from instagram_sdk.auth import REQUIRED_COOKIES, session_summary
from instagram_sdk.cursors import MAX_PAGES_PER_CALL, CursorState, decode_cursor, envelope
from instagram_sdk.endpoints import home, media, post, profile, saved
from instagram_sdk.endpoints._ids import normalize_username
from instagram_sdk.errors import AccountConfigError, AccountError
from instagram_sdk.models import (
    DownloadedMedia,
    MediaDownloadResult,
    MediaItem,
    SkippedMedia,
)
from instagram_sdk.runtime import Runtime
from instagram_sdk.settings import Settings

load_dotenv()

# FastMCP configures the root logger (INFO by default) when it is built, which
# made httpx log every CDN download URL — signed, with its query — to stderr.
# Pass our level instead, and keep the HTTP libraries at WARNING whatever it is.
_LOG_LEVEL = os.getenv("INSTAGRAM_LOG_LEVEL", "WARNING").strip().upper() or "WARNING"
if _LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    _LOG_LEVEL = "WARNING"
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

mcp = FastMCP("instagram-mcp", log_level=_LOG_LEVEL)

# Hard cap on every ``limit``: more posts means more requests on the account.
# Use ``next_cursor`` to read further.
MAX_LIMIT = 50

_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """The process runtime (accounts, browser, locks, state), created on first use."""
    global _runtime
    if _runtime is None:
        settings = Settings.from_env()
        _runtime = Runtime(settings, registry=AccountRegistry(settings))
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


async def _paged(
    tool: str,
    account: str | None,
    cursor: str | None,
    params: dict[str, Any],
    call: Callable[[Any, CursorState | None], Awaitable[Any]],
) -> dict[str, Any]:
    """Resolve the account, decode the cursor (before any network), run, wrap."""
    runtime = get_runtime()
    acct = runtime.resolve(account)
    start = decode_cursor(cursor, tool, acct.handle, params)
    page = await runtime.run(acct.handle, lambda ig: call(ig, start))
    return envelope(page, tool=tool, account=acct.handle, params=params, serialize=_serialize)


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
def _account_status(runtime: Runtime, acct: Account,
                    choice: DefaultChoice | None, default_error: str | None) -> dict[str, Any]:
    summary = session_summary(acct.session_path)
    is_default = choice is not None and choice.account.key == acct.key
    summary.update({
        "account": acct.handle,
        "handle": acct.handle,
        "is_default": is_default,
        "default_source": choice.source if is_default and choice else None,
        "user_id": acct.user_id,
        **runtime.state.summary(acct),
        "warning": (choice.warning if is_default and choice else None) or default_error,
    })
    return summary


def _default_choice(runtime: Runtime) -> tuple[DefaultChoice | None, str | None]:
    try:
        return runtime.registry.default(), None
    except AccountConfigError:
        raise
    except AccountError as exc:
        return None, str(exc)


@mcp.tool()
async def auth_status(account: str | None = None) -> dict[str, Any]:
    """Report whether an Instagram session is loaded and likely valid.

    Returns, for ``account`` (or the default account when omitted): the
    session file path, the handle, last-modified timestamp (proxy for last
    login), an ``authenticated`` flag based on a live (unexpired)
    ``sessionid`` cookie, ``expired_cookies`` (informational: ``ds_user_id``
    may expire long before the session and is not required), ``user_id``,
    ``is_default`` and ``default_source``
    (why it is the default), and ``state`` (``ok`` | ``blocked`` |
    ``cooldown`` | ``expired``) with ``block`` (kind, detail, until) if
    Instagram stopped the account earlier: a held account fails fast, without
    network, until it clears. ``warning`` explains a configuration mismatch
    (e.g. ``INSTAGRAM_USERNAME`` naming an account with no session) or why
    there is no default. See ``list_accounts`` for every account.

    This does NOT make a network request — to verify the session actually
    works, call any other tool. Session-expired errors there point back here.

    Args:
        account: Account handle (as in ``list_accounts``). Omit for the default.
    """
    runtime = get_runtime()
    choice, default_error = _default_choice(runtime)
    if account is not None and str(account).strip():
        acct = runtime.resolve(account)
    elif choice is not None:
        acct = choice.account
    else:
        return {
            "authenticated": False,
            "account": None,
            "handle": None,
            "session_path": None,
            "last_login": None,
            "missing_cookies": list(REQUIRED_COOKIES),
            "expired_cookies": [],
            "is_default": False,
            "default_source": None,
            "user_id": None,
            "state": None,
            "block": None,
            "warning": default_error,
            "available": [a.handle for a in runtime.registry.list()],
        }
    return _account_status(runtime, acct, choice, None if choice else default_error)


@mcp.tool()
async def list_accounts() -> list[dict[str, Any]]:
    """List every logged-in Instagram account and its state. No network.

    One entry per session file found, with the same fields as
    ``auth_status``. ``is_default`` marks the account used when a tool gets no
    ``account``. Returns ``[]`` when there are no sessions (log in with
    ``python -m scripts.auth_login --username <handle>``).
    """
    runtime = get_runtime()
    choice, default_error = _default_choice(runtime)
    return [
        _account_status(runtime, acct, choice, None if choice else default_error)
        for acct in runtime.registry.list()
    ]


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

@mcp.tool()
async def get_saved_posts(
    collection_id: str | None = None,
    limit: int = 24,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Read your saved posts (private to the account).

    Returns ``{items, next_cursor, stopped}``: ``items`` are posts;
    ``next_cursor`` (or null when there are no more) continues the list when
    passed back as ``cursor`` with the same other arguments; ``stopped`` says
    why this page ended (``end`` | ``limit`` | ``page_cap`` | ``budget``).

    Args:
        collection_id: Restrict to one save-collection (its id, from
            `get_saved_collections`). Omit for the full "All Posts" bucket.
        limit: Max posts to return (default 24, capped at 50). Keep this modest.
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account (a handle from `list_accounts`).
            Omit for the default account.
    """
    cid = collection_id.strip() if isinstance(collection_id, str) and collection_id.strip() else None
    return await _paged(
        "get_saved_posts", account, cursor, {"collection_id": cid},
        lambda ig, start: saved.fetch_saved_page(
            ig, collection_id=cid, limit=_clamp_limit(limit),
            start=start, max_pages=MAX_PAGES_PER_CALL,
        ),
    )


@mcp.tool()
async def get_saved_collections(account: str | None = None) -> list[dict[str, Any]]:
    """List your save-collections — each with its name and item count.

    The implicit "All Posts" bucket is included. Pass a collection's id to
    `get_saved_posts` to read just that collection.

    Args:
        account: Which logged-in account (a handle from `list_accounts`).
            Omit for the default account.
    """
    collections = await get_runtime().run(account, lambda ig: saved.fetch_collections(ig))
    return _serialize(collections)


@mcp.tool()
async def get_home_feed(
    limit: int = 15,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Read the account's home feed (timeline). Sponsored posts are dropped.

    Returns ``{items, next_cursor, stopped}`` (see `get_saved_posts`).

    Args:
        limit: Max posts to return (default 15, capped at 50). Keep this modest.
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account (a handle from `list_accounts`).
            Omit for the default account.
    """
    return await _paged(
        "get_home_feed", account, cursor, {},
        lambda ig, start: home.fetch_home_page(
            ig, limit=_clamp_limit(limit), start=start, max_pages=MAX_PAGES_PER_CALL
        ),
    )


@mcp.tool()
async def get_user_profile(username: str, account: str | None = None) -> dict[str, Any] | None:
    """Fetch a user's public profile.

    Returns bio, counters (posts, followers, following), private/verified
    flags, profile picture, external URL and bio links.

    Args:
        username: Instagram username without the leading "@".
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    result = await get_runtime().run(
        account, lambda ig: profile.fetch_profile(ig, username=username)
    )
    return _serialize(result)


@mcp.tool()
async def get_user_posts(
    username: str,
    limit: int = 24,
    include_reels: bool = False,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Fetch posts from a user's profile grid.

    Returns ``{items, next_cursor, stopped}`` (see `get_saved_posts`). On a
    profile that is mostly reels, ``stopped`` may be ``page_cap``: pass
    ``next_cursor`` back to keep looking.

    Args:
        username: Instagram username without the leading "@".
        limit: Max posts to return (default 24, capped at 50).
        include_reels: Keep reels in the result (excluded by default — use
            `get_user_reels` for a reels-only view).
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    handle = normalize_username(username)
    return await _paged(
        "get_user_posts", account, cursor,
        {"username": handle.casefold(), "include_reels": bool(include_reels)},
        lambda ig, start: profile.fetch_posts_page(
            ig, username=handle, limit=_clamp_limit(limit),
            include_reels=include_reels, start=start, max_pages=MAX_PAGES_PER_CALL,
        ),
    )


@mcp.tool()
async def get_post(id_or_url: str, account: str | None = None) -> dict[str, Any] | None:
    """Fetch one post by shortcode or URL.

    Returns the author, caption, media (images / videos / carousel slides) and
    like/comment counts, plus ``view_count`` (plays) for reels.

    Args:
        id_or_url: The post shortcode (e.g. "C8xY1zAbCdE") or a full
            https://www.instagram.com/p/<shortcode>/ URL. Reel URLs work too.
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    result = await get_runtime().run(
        account, lambda ig: post.fetch(ig, id_or_url=id_or_url)
    )
    return _serialize(result)


@mcp.tool()
async def get_user_reels(
    username: str,
    limit: int = 12,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Fetch reels from a user's Reels tab.

    Returns ``{items, next_cursor, stopped}`` (see `get_saved_posts`).

    Args:
        username: Instagram username without the leading "@".
        limit: Max reels to return (default 12, capped at 50).
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    handle = normalize_username(username)
    return await _paged(
        "get_user_reels", account, cursor, {"username": handle.casefold()},
        lambda ig, start: profile.fetch_reels_page(
            ig, username=handle, limit=_clamp_limit(limit),
            start=start, max_pages=MAX_PAGES_PER_CALL,
        ),
    )


@mcp.tool(structured_output=False)
async def download_media(
    id_or_url: str,
    indices: list[int] | None = None,
    download_videos: bool = False,
    account: str | None = None,
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
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.

    Returns:
        A list of inline images followed by one JSON summary:
        ``{shortcode, downloaded:[...], skipped:[...]}``. Saved files are
        ``<downloads>/<account>/post_<shortcode>_<pk>_<NN>.<ext>`` (downloads
        directory: ``~/.instagram-mcp/downloads/`` by default, or
        ``INSTAGRAM_DOWNLOADS_DIR``). The numeric ``pk`` keeps posts apart on
        case-insensitive file systems; re-downloading overwrites the same file.
    """
    runtime = get_runtime()
    acct = runtime.resolve(account)
    found = await runtime.run(
        acct.handle, lambda ig: media.resolve_post_for_download(ig, id_or_url=id_or_url)
    )
    shortcode, items = found.shortcode, found.media

    selected = _select_indices(items, indices)

    downloads_dir = Settings.from_env().downloads_dir
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
        path = downloader.media_path(downloads_dir, acct.handle, shortcode, found.pk, index, ext)
        downloader.write_atomic(path, outcome.data)

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
        level=_LOG_LEVEL,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    import anyio

    anyio.run(_amain)


if __name__ == "__main__":
    main()
