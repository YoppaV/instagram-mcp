"""Shared id/username parsers and URL builders.

The operations themselves (``doc_id``, friendly names, REST paths) live in
``instagram_sdk.ops``. A single post page still embeds its data in the
server-rendered HTML — that is ``EMBED_POST_KEY``.
"""

from __future__ import annotations

import re

BASE = "https://www.instagram.com"

# A single post page server-renders its data into the HTML under this key.
EMBED_POST_KEY = "xdt_api__v1__media__shortcode__web_info"

# Instagram's base64 alphabet for shortcodes.
_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Canonical Instagram shortcodes are 11 chars, but the home feed occasionally
# returns extended forms (carousel containers append slot metadata after an
# underscore, ~39 chars total). Both forms resolve to the same permalink — be
# permissive with the upper bound while keeping the character class strict.
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
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


def shortcode_to_pk(shortcode: str) -> str:
    """Numeric media id from a shortcode (base 64 over its first 11 chars)."""
    code = extract_shortcode(shortcode)[:11]
    value = 0
    for char in code:
        index = _SHORTCODE_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"invalid shortcode character {char!r}")
        value = value * 64 + index
    return str(value)
