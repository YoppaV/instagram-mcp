"""Shared id/username parsers, URL builders, and endpoint identifiers.

This is the single place to patch when Instagram rotates its endpoints — see
the "When the endpoints change" section of ``.claude/CLAUDE.md``.

Instagram's web app drives almost everything through ``/api/graphql`` POST
requests, distinguished by the ``fb_api_req_friendly_name`` form field — those
names are the ``FN_*`` constants. A few feeds are still plain REST endpoints
with a distinctive URL path — those are the ``FR_*`` constants. A single post
page embeds its data in the server-rendered HTML — that is ``EMBED_POST_KEY``.
"""

from __future__ import annotations

import math
import re

BASE = "https://www.instagram.com"

# GraphQL operations — matched against the request's fb_api_req_friendly_name.
# The home feed's first page is embedded in the HTML; this query paginates it.
FN_TIMELINE = "PolarisFeedRootPaginationCachedQuery_subscribe"
FN_PROFILE = "PolarisProfilePageContentQuery"
FN_PROFILE_POSTS = "PolarisProfilePostsQuery"
# The Reels tab fires two different friendly names: the first batch (~12
# items) comes from PolarisProfileReelsTabContentQuery on initial render,
# then each scroll-driven page is PolarisProfileReelsTabContentQuery_connection.
# Both share the same response shape (xdt_api__v1__clips__user__connection_v2).
FN_REELS: tuple[str, ...] = (
    "PolarisProfileReelsTabContentQuery",
    "PolarisProfileReelsTabContentQuery_connection",
)
FN_COLLECTIONS = "PolarisProfileSavedTabContentQuery"

# REST feeds — matched against a substring of the response URL.
FR_SAVED = "/api/v1/feed/saved/posts/"
FR_COLLECTION_FEED = "/api/v1/feed/collection/"

# A single post page server-renders its data into the HTML under this key.
EMBED_POST_KEY = "xdt_api__v1__media__shortcode__web_info"

# Hard ceiling on scroll iterations regardless of the requested limit.
MAX_SCROLLS_CAP = 60

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SHORTCODE_PATH_RE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def normalize_username(username: str) -> str:
    """Strip ``@`` and validate. Raise ValueError on invalid input."""
    handle = username.strip().lstrip("@").rstrip("/")
    if not _USERNAME_RE.match(handle):
        raise ValueError(
            f"Invalid Instagram username {username!r}. Must be 1–30 chars, "
            "letters/digits/periods/underscores only."
        )
    return handle


def extract_shortcode(id_or_url: str) -> str:
    """Return the post shortcode from a bare code or a post/reel URL."""
    raw = (id_or_url or "").strip()
    path_match = _SHORTCODE_PATH_RE.search(raw)
    if path_match:
        return path_match.group(1)
    if _SHORTCODE_RE.match(raw):
        return raw
    raise ValueError(
        f"Could not parse a post shortcode from {id_or_url!r}. Pass the "
        "shortcode or a full https://www.instagram.com/p/<shortcode>/ URL."
    )


def profile_url(username: str) -> str:
    return f"{BASE}/{username}/"


def post_url(shortcode: str) -> str:
    return f"{BASE}/p/{shortcode}/"


def reels_url(username: str) -> str:
    return f"{BASE}/{username}/reels/"


def saved_url(handle: str) -> str:
    """The saved-posts landing page — shows the list of collections."""
    return f"{BASE}/{handle}/saved/"


def saved_all_url(handle: str) -> str:
    """The implicit "All Posts" saved bucket."""
    return f"{BASE}/{handle}/saved/all-posts/"


def collection_url(handle: str, collection_id: str) -> str:
    """A specific saved collection's page.

    Instagram's route is ``/saved/<slug>/<id>/`` and it ignores the slug (it
    redirects to the canonical one), so the id is repeated as a placeholder.
    """
    return f"{BASE}/{handle}/saved/{collection_id}/{collection_id}/"


def scroll_budget(limit: int | None) -> int:
    """How many scroll iterations to allow for a requested ``limit``.

    Instagram serves roughly a dozen items per page; allow a little slack, but
    never exceed ``MAX_SCROLLS_CAP`` — runaway scrolling is exactly what trips
    automation defenses.
    """
    if not limit or limit <= 0:
        return MAX_SCROLLS_CAP
    return min(MAX_SCROLLS_CAP, math.ceil(limit / 10) + 4)
