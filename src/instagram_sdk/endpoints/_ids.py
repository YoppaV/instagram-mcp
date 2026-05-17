"""Shared id/username parsers, URL builders, and ``/api/v1`` path fragments.

This is the single place to patch when Instagram rotates its endpoint paths —
see the "When the endpoints change" section of ``.claude/CLAUDE.md``.
"""

from __future__ import annotations

import math
import re

BASE = "https://www.instagram.com"

# Path fragments matched against intercepted response URLs. Each is a stable
# substring of an ``/api/v1/...`` (or ``/graphql``) request the web app fires.
FR_TIMELINE = "/api/v1/feed/timeline/"
FR_WEB_PROFILE = "/api/v1/users/web_profile_info/"
FR_USER_FEED = "/api/v1/feed/user/"
FR_GRAPHQL = "/graphql/query"
FR_CLIPS = "/api/v1/clips/user/"
FR_SAVED = "/api/v1/feed/saved/posts/"
FR_COLLECTION_FEED = "/api/v1/feed/collection/"
FR_COLLECTIONS_LIST = "/api/v1/collections/list/"

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
    """The saved-posts landing page — fires the collections list."""
    return f"{BASE}/{handle}/saved/"


def saved_all_url(handle: str) -> str:
    """The implicit "All Posts" saved bucket."""
    return f"{BASE}/{handle}/saved/all-posts/"


def collection_url(handle: str, collection_id: str) -> str:
    """A specific saved collection's page.

    Instagram's canonical URL also carries a name slug
    (``/saved/<slug>/<id>/``); the bare-id form generally redirects there.
    If a collection ever fails to load, confirm the live URL in DevTools.
    """
    return f"{BASE}/{handle}/saved/{collection_id}/"


def scroll_budget(limit: int | None) -> int:
    """How many scroll iterations to allow for a requested ``limit``.

    Instagram serves roughly ten items per paginated request; allow a little
    slack, but never exceed ``MAX_SCROLLS_CAP`` — runaway scrolling is exactly
    what trips automation defenses.
    """
    if not limit or limit <= 0:
        return MAX_SCROLLS_CAP
    return min(MAX_SCROLLS_CAP, math.ceil(limit / 10) + 2)
