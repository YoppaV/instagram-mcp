"""End-to-end tests for the FastMCP server with mocked browser/endpoints.

We import the server module, replace its runtime with a fake that hands an
opaque client to each tool, and patch the endpoint modules to return canned
dataclasses.
Asserts that the registered tools serialize results into plain dicts (which is
what MCP clients see).

Run with: ``venv/bin/python -m unittest tests.test_server -v``
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import server as server_mod  # noqa: E402
from instagram_sdk.cursors import PageResult  # noqa: E402
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


class FakeRuntime:
    """Stands in for ``Runtime``: runs ``fn`` with an opaque client."""

    def __init__(self, handle: str = "carol") -> None:
        self.accounts: list[Any] = []
        self.handle = handle

    def resolve(self, account: Any) -> Any:
        from instagram_sdk.accounts import Account

        return Account(account or self.handle, Path(f"/x/{account or self.handle}_instagram_state.json"))

    async def run(self, account: Any, fn: Any) -> Any:
        self.accounts.append(account)
        return await fn(object())  # endpoints are mocked, never touch it


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _patch_browser() -> "mock._patch":
    return mock.patch.object(server_mod, "get_runtime", return_value=FakeRuntime())


def _page(items: list[Any]) -> PageResult:
    return PageResult(items, None, "end")


class AuthStatusToolTests(unittest.TestCase):
    def _runtime(self, tmp: str, **env: Any) -> Any:
        from dataclasses import replace

        from instagram_sdk.accounts import AccountRegistry
        from instagram_sdk.runtime import Runtime
        from instagram_sdk.settings import Settings

        fields = {"session_file": None, "username": "", "default_account": "", **env}
        settings = replace(Settings.from_env(), state_dir=Path(tmp) / "state",
                           session_dir=Path(tmp) / "sessions", **fields)
        return Runtime(settings, registry=AccountRegistry(settings))

    def _session(self, tmp: str, handle: str) -> Path:
        d = Path(tmp) / "sessions"
        d.mkdir(exist_ok=True)
        path = d / f"{handle}_instagram_state.json"
        path.write_text(json.dumps({"cookies": [
            {"name": "sessionid", "value": "S"}, {"name": "ds_user_id", "value": "77"}]}))
        return path

    def test_no_sessions(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            with mock.patch.object(server_mod, "get_runtime", return_value=runtime):
                result = _run(server_mod.auth_status())
                listed = _run(server_mod.list_accounts())
        self.assertFalse(result["authenticated"])
        self.assertIsNone(result["account"])
        self.assertIn("auth_login", result["warning"])
        self.assertEqual(listed, [])

    def test_reports_default_and_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._session(tmp, "carol")
            runtime = self._runtime(tmp, username="someone_else")
            with mock.patch.object(server_mod, "get_runtime", return_value=runtime):
                result = _run(server_mod.auth_status())
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["account"], "carol")
        self.assertEqual(result["session_path"], str(path))
        self.assertEqual(result["user_id"], "77")
        self.assertTrue(result["is_default"])
        self.assertEqual(result["default_source"], "only_session")
        self.assertIn("INSTAGRAM_USERNAME=someone_else", result["warning"])
        self.assertEqual(result["state"], "ok")
        self.assertIsNone(result["block"])

    def test_list_accounts_and_explicit_account(self) -> None:
        import tempfile

        from instagram_sdk.errors import UnknownAccountError
        from instagram_sdk.guard import BlockSignal

        with tempfile.TemporaryDirectory() as tmp:
            self._session(tmp, "carol")
            self._session(tmp, "dave")
            runtime = self._runtime(tmp, default_account="dave")
            runtime.state.record(runtime.resolve("carol"), BlockSignal("checkpoint", "x"))
            with mock.patch.object(server_mod, "get_runtime", return_value=runtime):
                listed = _run(server_mod.list_accounts())
                carol = _run(server_mod.auth_status(account="@Carol"))
                with self.assertRaises(UnknownAccountError):
                    _run(server_mod.auth_status(account="zed"))
        self.assertEqual([a["account"] for a in listed], ["carol", "dave"])
        self.assertEqual([a["is_default"] for a in listed], [False, True])
        self.assertEqual(listed[1]["default_source"], "default_account")
        self.assertEqual(carol["state"], "blocked")
        self.assertEqual(carol["block"]["kind"], "checkpoint")
        # The block on carol pauses dave too (shared IP and browser).
        self.assertEqual(listed[1]["state"], "cooldown")
        self.assertEqual(listed[1]["block"]["kind"], "global_cooldown")

    def test_ambiguous_default_is_reported_not_guessed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._session(tmp, "carol")
            self._session(tmp, "dave")
            runtime = self._runtime(tmp)
            with mock.patch.object(server_mod, "get_runtime", return_value=runtime):
                result = _run(server_mod.auth_status())
        self.assertIsNone(result["account"])
        self.assertEqual(result["available"], ["carol", "dave"])
        self.assertIn("INSTAGRAM_DEFAULT_ACCOUNT", result["warning"])


class GetHomeFeedToolTests(unittest.TestCase):
    def test_serializes_posts(self) -> None:
        fetch = mock.AsyncMock(return_value=_page([_make_post("1_2"), _make_post("3_4")]))
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.home.fetch_home_page", new=fetch
        ):
            result = _run(server_mod.get_home_feed(limit=5))
        self.assertEqual(set(result), {"items", "next_cursor", "stopped"})
        self.assertEqual([p["id"] for p in result["items"]], ["1_2", "3_4"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["stopped"], "end")
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
        fetch = mock.AsyncMock(return_value=_page([_make_post("9_9")]))
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.profile.fetch_posts_page", new=fetch
        ):
            result = _run(
                server_mod.get_user_posts(
                    username="carol", limit=7, include_reels=True
                )
            )
        self.assertEqual(result["items"][0]["id"], "9_9")
        self.assertEqual(fetch.call_args.kwargs["include_reels"], True)
        self.assertEqual(fetch.call_args.kwargs["limit"], 7)


class GetUserReelsToolTests(unittest.TestCase):
    def test_serializes_reels(self) -> None:
        fetch = mock.AsyncMock(return_value=_page([_make_reel("5_5")]))
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.profile.fetch_reels_page", new=fetch
        ):
            result = _run(server_mod.get_user_reels(username="carol"))
        self.assertEqual(result["items"][0]["id"], "5_5")
        self.assertEqual(result["items"][0]["play_count"], 9999)
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
    def test_no_handle_needed(self) -> None:
        fetch = mock.AsyncMock(return_value=_page([_make_post("6001_70")]))
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.saved.fetch_saved_page", new=fetch
        ):
            result = _run(server_mod.get_saved_posts(limit=10))
        self.assertEqual(result["items"][0]["id"], "6001_70")
        self.assertNotIn("handle", fetch.call_args.kwargs)
        self.assertIsNone(fetch.call_args.kwargs["collection_id"])
        self.assertEqual(fetch.call_args.kwargs["max_pages"], 8)

    def test_passes_collection_id(self) -> None:
        fetch = mock.AsyncMock(return_value=_page([]))
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.saved.fetch_saved_page", new=fetch
        ):
            _run(server_mod.get_saved_posts(collection_id="17900000000000001"))
        self.assertEqual(
            fetch.call_args.kwargs["collection_id"], "17900000000000001"
        )


class GetSavedCollectionsToolTests(unittest.TestCase):
    def test_serializes_collections(self) -> None:
        fetch = mock.AsyncMock(return_value=[_make_collection()])
        with _patch_browser(), mock.patch(
            "instagram_sdk.server.saved.fetch_collections", new=fetch
        ):
            result = _run(server_mod.get_saved_collections())
        self.assertEqual(result[0]["name"], "Recipes")
        json.dumps(result)


class AccountAndCursorTests(unittest.TestCase):
    def test_account_reaches_the_runtime(self) -> None:
        runtime = FakeRuntime()
        fetch = mock.AsyncMock(return_value=_make_profile())
        with mock.patch.object(server_mod, "get_runtime", return_value=runtime), \
                mock.patch("instagram_sdk.server.profile.fetch_profile", new=fetch):
            _run(server_mod.get_user_profile(username="x", account="dave"))
            _run(server_mod.get_user_profile(username="x"))
        self.assertEqual(runtime.accounts, ["dave", None])

    def test_cursor_round_trip(self) -> None:
        from instagram_sdk.cursors import CursorState

        runtime = FakeRuntime()
        first = mock.AsyncMock(return_value=PageResult([_make_post("1_1")],
                                                       CursorState("UP2", 0), "limit"))
        with mock.patch.object(server_mod, "get_runtime", return_value=runtime), \
                mock.patch("instagram_sdk.server.profile.fetch_posts_page", new=first):
            out = _run(server_mod.get_user_posts(username="@Carol", limit=1))
            self.assertIsNone(first.call_args.kwargs["start"])
            cursor = out["next_cursor"]
            self.assertIsInstance(cursor, str)
            _run(server_mod.get_user_posts(username="carol", limit=1, cursor=cursor))
            self.assertEqual(first.call_args.kwargs["start"], CursorState("UP2", 0))
            # Same cursor, other query / other account / other tool → rejected
            # before any request.
            calls = first.call_count
            for kwargs in ({"username": "dave"}, {"username": "carol", "include_reels": True},
                           {"username": "carol", "account": "dave"}):
                with self.subTest(**kwargs), self.assertRaises(ValueError):
                    _run(server_mod.get_user_posts(cursor=cursor, **kwargs))
            with self.assertRaises(ValueError):
                _run(server_mod.get_user_reels(username="carol", cursor=cursor))
            with self.assertRaises(ValueError):
                _run(server_mod.get_user_posts(username="carol", cursor="garbage!"))
        self.assertEqual(first.call_count, calls)


class ToolRegistrationTests(unittest.TestCase):
    def test_all_tools_registered(self) -> None:
        tools = _run(server_mod.mcp.list_tools())
        names = {t.name for t in tools}
        expected = {
            "auth_status",
            "list_accounts",
            "get_saved_posts",
            "get_saved_collections",
            "get_home_feed",
            "get_user_profile",
            "get_user_posts",
            "get_post",
            "get_user_reels",
            "download_media",
            "get_post_comments",
            "search_users",
            "search_hashtag",
            "start_bulk_download",
            "bulk_download_status",
            "cancel_bulk_download",
        }
        self.assertEqual(names, expected)


if __name__ == "__main__":
    unittest.main()
