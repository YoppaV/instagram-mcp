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
from instagram_sdk.transport import TransportConfigError, TransportSettings, serve_http

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


# --------------------------------------------------------------------------
# Comments and search
# --------------------------------------------------------------------------
# Imported here, not at the top, to keep this section self-contained.
from instagram_sdk.endpoints import comments, search  # noqa: E402

# Search has a small budget of its own: few pages per call.
SEARCH_MAX_LIMIT = 50


@mcp.tool()
async def get_post_comments(
    id_or_url: str,
    limit: int = 20,
    cursor: str | None = None,
    include_replies: bool = False,
    account: str | None = None,
) -> dict[str, Any]:
    """Read a post's comments (top-level, in Instagram's "popular" order).

    Returns ``{items, next_cursor, stopped}`` (see `get_saved_posts`). Each
    comment has ``pk``, ``text``, ``created_at`` (unix seconds),
    ``like_count``, ``reply_count``, ``is_pinned``, ``is_edited`` and
    ``author``. The first page brings 15 comments, then 10 per request.

    With ``include_replies`` the first replies are attached as ``replies``,
    but only for the first 3 comments that have any, 5 replies each (one
    extra request per thread). ``replies`` is ``[]`` for a comment with no
    replies and null when they were not fetched; ``reply_count`` is always
    the full number. Comments count in a separate, smaller budget.

    Args:
        id_or_url: The post shortcode, a post/reel URL, or its numeric media id.
        limit: Max comments to return (default 20, capped at 50).
        cursor: ``next_cursor`` from a previous call, to continue (with or
            without ``include_replies``).
        include_replies: Attach a small sample of replies (see above).
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    media_id = comments.media_id_of(id_or_url)
    return await _paged(
        "get_post_comments", account, cursor,
        # include_replies does not change which comments come: not in the query.
        {"media_id": media_id},
        lambda ig, start: comments.fetch_comments_page(
            ig, id_or_url=media_id, limit=_clamp_limit(limit),
            include_replies=bool(include_replies), start=start,
            max_pages=MAX_PAGES_PER_CALL,
        ),
    )


@mcp.tool()
async def search_users(query: str, limit: int = 10, account: str | None = None) -> list[dict[str, Any]]:
    """Search accounts by name or username (Instagram's search box).

    One request. Returns users in Instagram's ranking, each with ``pk``,
    ``username``, ``full_name``, ``is_verified`` and ``profile_pic_url``
    (``is_private`` is not in this result; it reads false). Search has its
    own small budget: avoid firing many searches in a row.

    Args:
        query: Free text, e.g. "national geographic" or "natgeo".
        limit: Max users to return (default 10, capped at 50).
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    text = search.normalize_query(query)
    lim = max(1, min(limit, SEARCH_MAX_LIMIT))
    users = await get_runtime().run(
        account, lambda ig: search.search_users(ig, query=text, limit=lim)
    )
    return _serialize(users)


@mcp.tool()
async def search_hashtag(
    tag: str,
    limit: int = 24,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Posts for a hashtag, as Instagram's search ranks them (top results).

    The classic hashtag page no longer exists; this is the keyword search
    for ``#tag``. Returns ``{items, next_cursor, stopped}`` (see
    `get_saved_posts`); items are posts (mostly reels). About 24 posts per
    request, at most 3 requests per call. Search has its own small budget.

    Args:
        tag: The hashtag, with or without the leading "#" (e.g. "travel").
        limit: Max posts to return (default 24, capped at 50).
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    norm = search.normalize_tag(tag)
    return await _paged(
        "search_hashtag", account, cursor, {"tag": norm},
        lambda ig, start: search.fetch_hashtag_page(
            ig, tag=norm, limit=max(1, min(limit, SEARCH_MAX_LIMIT)), start=start,
            max_pages=search.HASHTAG_MAX_PAGES,
        ),
    )


# --------------------------------------------------------------------------
# Bulk downloads (background jobs; see instagram_sdk.bulk)
# --------------------------------------------------------------------------
from instagram_sdk import bulk as _bulk  # noqa: E402

# job_id -> task. Kept so the tasks are not garbage-collected and can be
# stopped cleanly when the server exits.
_bulk_tasks: dict[str, Any] = {}


def _downloads_dir() -> Any:
    return Settings.from_env().downloads_dir


@mcp.tool()
async def start_bulk_download(
    source: str,
    target: str | None = None,
    download_videos: bool = False,
    mode: str = "auto",
    known_stop: int | None = None,
    max_pages: int | None = None,
    max_posts: int | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Start (or resume) a background bulk download; returns at once.

    Saves every post of a source to ``<downloads>/<account>/`` without
    repeating what is already there (a per-account manifest keyed by post id
    and media index, ``manifest.sqlite3`` in that folder). The job runs in
    the background in small paced batches (one feed page each), respecting
    the account's hourly/daily budget and leaving a reserve for other tools;
    it may take hours for a large source. Media URLs come from the feed
    itself, so there is no extra request per post. Returns a status summary,
    never images. Follow it with `bulk_download_status`; stop it with
    `cancel_bulk_download`. If the server stops, the job is resumable: call
    this again with the same arguments. One job per account at a time.

    Args:
        source: ``saved`` (all saved posts), ``collection`` (one
            save-collection) or ``user_posts`` (a profile's whole grid,
            reels included).
        target: The collection id (from `get_saved_collections`) for
            ``collection``; the username for ``user_posts``.
        download_videos: Also save videos (streamed to disk). Off by default:
            videos are recorded as skipped and fetched by a later run with
            this on.
        mode: ``auto`` (default: first run walks everything and saves a
            resume point; later runs scan the newest posts, stop after
            ``known_stop`` already-saved ones in a row, then finish any
            unfinished first walk), ``incremental`` (only the newest posts) or
            ``full`` (walk everything again; saved files are not re-fetched).
        known_stop: Consecutive already-saved posts that end the scan of the
            newest posts (default 8).
        max_pages: Stop this run after this many feed pages (resume later).
        max_posts: Stop this run after this many new posts (resume later).
        account: Which logged-in account (a handle from `list_accounts`).
            Omit for the default account.
    """
    import asyncio

    runtime = get_runtime()
    acct = runtime.resolve(account)
    spec = _bulk.make_spec(source, target, download_videos=download_videos, mode=mode,
                           known_stop=known_stop, max_pages=max_pages, max_posts=max_posts)
    runtime.state.check(acct)  # a held account fails now, not in the background
    job = _bulk.BulkJob(runtime, acct.handle, spec, downloads_dir=_downloads_dir())
    try:
        status = job.claim()
    except _bulk.BulkJobBusyError as exc:
        raise ValueError(str(exc)) from None
    task = asyncio.get_running_loop().create_task(job.run(), name=f"bulk:{job.job_id}")
    _bulk_tasks[job.job_id] = task
    task.add_done_callback(lambda t, jid=job.job_id: _bulk_tasks.pop(jid, None))
    return status


@mcp.tool()
async def bulk_download_status(
    job_id: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Progress of bulk downloads. No network.

    Returns ``{jobs: [...]}``; each job has ``status`` (``running`` |
    ``waiting`` for budget | ``done`` | ``partial`` (a run cap was reached) |
    ``cancelled`` | ``error`` | ``interrupted`` (the server stopped; start it
    again to resume)), ``this_run`` counters (pages, posts_new, downloaded,
    skipped, failed, bytes...), ``manifest_totals`` for the source,
    ``resumable``, ``error`` and the manifest path.

    Args:
        job_id: One job (as returned by `start_bulk_download`). Omit to list.
        account: List only this account's jobs. Omit (with no job_id) for all.
    """
    downloads_dir = _downloads_dir()
    if job_id:
        found = _bulk.job_status(downloads_dir, job_id)
        if found is None:
            raise ValueError(f"no bulk job {job_id!r}")
        return {"jobs": [found]}
    runtime = get_runtime()
    if account is not None and str(account).strip():
        handles = [runtime.resolve(account).handle]
    else:
        handles = [a.handle for a in runtime.registry.list()]
    return {"jobs": _bulk.list_jobs(downloads_dir, handles)}


@mcp.tool()
async def cancel_bulk_download(job_id: str) -> dict[str, Any]:
    """Stop a bulk download (within a few seconds, from any process).

    What was saved stays saved and the resume point is kept: calling
    `start_bulk_download` again with the same source continues. No network.

    Args:
        job_id: The job, as returned by `start_bulk_download`.
    """
    found = _bulk.request_cancel(_downloads_dir(), job_id)
    if found is None:
        raise ValueError(f"no bulk job {job_id!r}")
    return found


async def _stop_bulk_tasks() -> None:
    import asyncio

    tasks = list(_bulk_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------
# Followers / following
# --------------------------------------------------------------------------
from instagram_sdk.endpoints import friendships  # noqa: E402


async def _follow_list(kind: str, username: str, limit: int, cursor: str | None,
                       account: str | None) -> dict[str, Any]:
    handle = normalize_username(username)
    return await _paged(
        f"get_{kind}", account, cursor, {"username": handle.casefold()},
        lambda ig, start: friendships.fetch_follow_page(
            ig, kind, username=handle, limit=_clamp_limit(limit), start=start,
        ),
    )


@mcp.tool()
async def get_followers(
    username: str,
    limit: int = 24,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """List the accounts that follow a user.

    Returns ``{items, next_cursor, stopped}`` with one user per item (``pk``,
    ``username``, ``full_name``, ``is_verified``, ``is_private``,
    ``profile_pic_url``). This is the request Instagram watches most: each
    call reads at most 5 pages of 12 (60 users), waits 30 s after the previous
    followers/following call of the account, and the account has its own
    budget (30 pages per hour, 100 per day). It never chains by itself — pass
    ``next_cursor`` back to read on. Exporting the followers of large
    accounts is not a goal. A private account you do not follow fails with a
    clear error before any list request.

    Args:
        username: Instagram username without the leading "@".
        limit: Max users to return (default 24, capped at 50).
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    return await _follow_list("followers", username, limit, cursor, account)


@mcp.tool()
async def get_following(
    username: str,
    limit: int = 24,
    cursor: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """List the accounts a user follows.

    Same shape, limits and budget as `get_followers` (they share it).

    Args:
        username: Instagram username without the leading "@".
        limit: Max users to return (default 24, capped at 50).
        cursor: ``next_cursor`` from a previous call, to continue.
        account: Which logged-in account makes the request (a handle from
            `list_accounts`). Omit for the default account.
    """
    return await _follow_list("following", username, limit, cursor, account)


async def _amain(transport_settings: TransportSettings | None = None) -> None:
    try:
        if transport_settings is not None and transport_settings.is_http:
            await serve_http(mcp, transport_settings)
        else:
            await mcp.run_stdio_async()
    finally:
        # Background bulk jobs first: they end as "interrupted" (resumable).
        await _stop_bulk_tasks()
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

    # Read before starting anything: a bad transport config (HTTP without a
    # token, a '*' host) stops here with a message, not halfway up.
    try:
        transport_settings = TransportSettings.from_env()
    except TransportConfigError as exc:
        print(f"instagram-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    anyio.run(_amain, transport_settings)


if __name__ == "__main__":
    main()
