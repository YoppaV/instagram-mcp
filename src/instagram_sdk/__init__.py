"""Instagram SDK exposed as an MCP server.

Read-only access to saved posts, the home feed, individual posts, user
profiles, their posts and reels — via in-page ``fetch`` calls from one warm,
resource-blocked Playwright tab. See ``instagram_sdk.server`` for the MCP
entrypoint.
"""

import logging as _logging

from .models import Collection, MediaItem, Post, Profile, Reel, User

__all__ = [
    "Collection",
    "MediaItem",
    "Post",
    "Profile",
    "Reel",
    "User",
]

# Library default: silent unless the application configures logging.
_logging.getLogger("instagram_sdk").addHandler(_logging.NullHandler())
