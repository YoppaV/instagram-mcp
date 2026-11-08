"""End-to-end tests for the FastMCP server with mocked browser/endpoints.

We import the server module, replace its ``_browser`` with a fake that yields
an opaque page, and patch the endpoint modules to return canned dataclasses.
Asserts that the registered tools serialize results into plain dicts (which is
what MCP clients see).

Run with: ``venv/bin/python -m unittest tests.test_server -v``
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import server as server_mod  # noqa: E402
from instagram_sdk.auth import SessionExpiredError  # noqa: E402
from instagram_sdk.models import (  # noqa: E402
    Collection,
    MediaItem,
    Post,
    Profile,
    Reel,
    User,
)


def _make_user(username: str = "carol", pk: str = "500") -> User:
    return User(
        pk=pk,
        username=username,
        full_name=username.title(),
        is_verified=True,
        is_private=False,
        profile_pic_url="https://cdn.example.com/carol.jpg",
    )


def _make_post(post_id: str, shortcode: str = "AAA1") -> Post:
    return Post(
        pk=post_id.split("_")[0],
        id=post_id,
        shortcode=shortcode,
        url=f"https://www.instagram.com/p/{shortcode}/",
        media_type=1,
        caption="caption",
        media=(MediaItem(1, "https://cdn.example.com/x.jpg", None, 1080, 1080),),
        like_count=10,
        comment_count=2,
        view_count=None,
        taken_at=1700000000,
        product_type="feed",
        location=None,
        author=_make_user(),
    )


def _make_reel(reel_id: str) -> Reel:
    return Reel(
        pk=reel_id.split("_")[0],
        id=reel_id,
        shortcode="RL1",
        url="https://www.instagram.com/p/RL1/",
        caption="reel",
        video_url="https://cdn.example.com/r.mp4",
        thumbnail_url="https://cdn.example.com/r.jpg",
        like_count=200,
        comment_count=10,
        play_count=9999,
        taken_at=1700000000,
        author=_make_user(),
    )


def _make_profile() -> Profile:
    return Profile(
        pk="500",
        username="carol",
        full_name="Carol Doe",
        biography="bio",
        external_url=None,
        bio_links=(),
        follower_count=4200,
        following_count=311,
        post_count=87,
        is_private=False,
        is_verified=True,
        is_business=False,
        profile_pic_url="https://cdn.example.com/carol.jpg",
    )


def _make_collection(name: str = "Recipes") -> Collection:
    return Collection(
        collection_id="17900000000000001",
        name=name,
        media_count=15,
        cover_image_url="https://cdn.example.com/cover.jpg",
    )


class FakeBrowser:
    @asynccontextmanager
    async def page(self) -> Any:
        yield object()  # opaque — endpoints are mocked, never touch the page


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _patch_browser() -> "mock._patch":
    return mock.patch.object(server_mod, "_browser", FakeBrowser())


def _patch_session(name: str = "carol_instagram_state.json") -> "mock._patch":
    return mock.patch.object(
        server_mod, "_resolve_session_path", return_value=Path("sessions") / name
    )


class AuthStatusToolTests(unittest.TestCase):
    def test_reports_session_summary(self) -> None:
        missing = Path("/nope")
        with mock.patch.object(
            server_mod, "_resolve_session_path", return_value=missing
        ):
            result = _run(server_mod.auth_status())
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["session_path"], str(missing))


class ResolveOwnHandleTests(unittest.TestCase):
    def test_derives_handle_from_filename(self) -> None:
        with _patch_session("alice_instagram_state.json"):
            self.assertEqual(server_mod._resolve_own_handle(), "alice")

    def test_rejects_malformed_filename(self) -> None:
        with _patch_session("instagram_state.json"):
            with self.assertRaises(SessionExpiredError):
                server_mod._resolve_own_handle()


class GetHomeFeedToolTests(unittest.TestCase):
    def test_serializes_posts(self) -> None:
        fetch = mock.AsyncMock(return_value=[_make_post("1_2"), _make_post("3_4")])
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.home.fetch", new=fetch
        ):
            result = _run(server_mod.get_home_feed(limit=5))
        self.assertEqual([p["id"] for p in result], ["1_2", "3_4"])
        self.assertEqual(fetch.call_args.kwargs["limit"], 5)
        json.dumps(result)


class GetUserProfileToolTests(unittest.TestCase):
    def test_returns_profile_dict(self) -> None:
        fetch = mock.AsyncMock(return_value=_make_profile())
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.profile.fetch_profile", new=fetch
        ):
            result = _run(server_mod.get_user_profile(username="carol"))
        self.assertEqual(result["username"], "carol")
        self.assertEqual(result["follower_count"], 4200)

    def test_none_when_missing(self) -> None:
        fetch = mock.AsyncMock(return_value=None)
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.profile.fetch_profile", new=fetch
        ):
            result = _run(server_mod.get_user_profile(username="ghost"))
        self.assertIsNone(result)


class GetUserPostsToolTests(unittest.TestCase):
    def test_passes_include_reels(self) -> None:
        fetch = mock.AsyncMock(return_value=[_make_post("9_9")])
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.profile.fetch_posts", new=fetch
        ):
            result = _run(
                server_mod.get_user_posts(
                    username="carol", limit=7, include_reels=True
                )
            )
        self.assertEqual(result[0]["id"], "9_9")
        self.assertEqual(fetch.call_args.kwargs["include_reels"], True)
        self.assertEqual(fetch.call_args.kwargs["limit"], 7)


class GetUserReelsToolTests(unittest.TestCase):
    def test_serializes_reels(self) -> None:
        fetch = mock.AsyncMock(return_value=[_make_reel("5_5")])
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.profile.fetch_reels", new=fetch
        ):
            result = _run(server_mod.get_user_reels(username="carol"))
        self.assertEqual(result[0]["id"], "5_5")
        self.assertEqual(result[0]["play_count"], 9999)
        json.dumps(result)


class GetPostToolTests(unittest.TestCase):
    def test_returns_post_dict(self) -> None:
        fetch = mock.AsyncMock(return_value=_make_post("4001_500", "PI1"))
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.post.fetch", new=fetch
        ):
            result = _run(server_mod.get_post(id_or_url="PI1"))
        self.assertEqual(result["id"], "4001_500")
        self.assertEqual(result["media"][0]["image_url"], "https://cdn.example.com/x.jpg")

    def test_none_when_missing(self) -> None:
        fetch = mock.AsyncMock(return_value=None)
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.post.fetch", new=fetch
        ):
            result = _run(server_mod.get_post(id_or_url="ZZZ"))
        self.assertIsNone(result)


class GetSavedPostsToolTests(unittest.TestCase):
    def test_uses_derived_handle(self) -> None:
        fetch = mock.AsyncMock(return_value=[_make_post("6001_70")])
        with _patch_browser(), _patch_session(), mock.patch(
            "instagram_sdk.server.saved.fetch_saved", new=fetch
        ):
            result = _run(server_mod.get_saved_posts(limit=10))
        self.assertEqual(result[0]["id"], "6001_70")
        self.assertEqual(fetch.call_args.kwargs["handle"], "carol")
        self.assertIsNone(fetch.call_args.kwargs["collection_id"])

    def test_passes_collection_id(self) -> None:
        fetch = mock.AsyncMock(return_value=[])
        with _patch_browser(), _patch_session(), mock.patch(
            "instagram_sdk.server.saved.fetch_saved", new=fetch
        ):
            _run(server_mod.get_saved_posts(collection_id="17900000000000001"))
        self.assertEqual(
            fetch.call_args.kwargs["collection_id"], "17900000000000001"
        )


class GetSavedCollectionsToolTests(unittest.TestCase):
    def test_serializes_collections(self) -> None:
        fetch = mock.AsyncMock(return_value=[_make_collection()])
        with _patch_browser(), _patch_session(), mock.patch(
            "instagram_sdk.server.saved.fetch_collections", new=fetch
        ):
            result = _run(server_mod.get_saved_collections())
        self.assertEqual(result[0]["name"], "Recipes")
        self.assertEqual(fetch.call_args.kwargs["handle"], "carol")
        json.dumps(result)


class ToolRegistrationTests(unittest.TestCase):
    def test_all_tools_registered(self) -> None:
        tools = _run(server_mod.mcp.list_tools())
        names = {t.name for t in tools}
        expected = {
            "auth_status",
            "get_saved_posts",
            "get_saved_collections",
            "get_home_feed",
            "get_user_profile",
            "get_user_posts",
            "get_post",
            "get_user_reels",
            "download_media",
        }
        self.assertEqual(names, expected)


if __name__ == "__main__":
    unittest.main()
