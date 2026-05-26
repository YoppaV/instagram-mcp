"""Instagram SDK exposed as an MCP server.

Read-only access to saved posts, the home feed, individual posts, user
profiles, their posts and reels — via Playwright + ``/api/v1/...`` JSON
response interception. See ``instagram_sdk.server`` for the MCP entrypoint.
"""

from .models import Collection, MediaItem, Post, Profile, Reel, User

__all__ = [
    "Collection",
    "MediaItem",
    "Post",
    "Profile",
    "Reel",
    "User",
]
