"""Endpoint integration tests with a faked Playwright Page.

Each test stands up a ``FakePage`` that records response handlers, then
synthesizes ``FakeResponse`` objects carrying captured ``/api/v1`` fixtures and
dispatches them through the handler on ``goto``. The endpoint code is exercised
end-to-end except for real browser IO and scrolling sleeps (``asyncio.sleep``
is monkey-patched to a no-op).

Run with: ``venv/bin/python -m unittest tests.test_endpoints -v``
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import scraper as scraper_mod  # noqa: E402
from instagram_sdk.endpoints import home, post, profile, saved  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    """Mimics a playwright.async_api.Response for the response handler."""

    def __init__(self, url: str, payload: dict[str, Any]) -> None:
        self.url = url
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload


class FakePage:
    """Minimal Page double.

    The endpoint installs a ``response`` listener; the page fires synthetic
    responses through it on ``goto``.
    """

    def __init__(
        self,
        *,
        url: str = "https://www.instagram.com/",
        responses: list[FakeResponse] | None = None,
    ) -> None:
        self.url = url
        self._responses = responses or []
        self._listeners: dict[str, list[Callable[..., Awaitable[None]]]] = {}
        self.scroll_calls = 0
        self.evaluate_calls: list[str] = []
        self.goto_calls: list[str] = []

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[..., Any]) -> None:
        self._listeners.get(event, []).remove(handler)

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_calls.append(url)
        self.url = url
        await self._fire_responses()

    async def evaluate(self, script: str) -> None:
        self.evaluate_calls.append(script)
        self.scroll_calls += 1

    async def _fire_responses(self) -> None:
        for response in self._responses:
            for handler in list(self._listeners.get("response", [])):
                await handler(response)


def _no_sleep_patch() -> "mock._patch":
    async def _zero(*_: Any, **__: Any) -> None:
        return None

    return mock.patch.object(scraper_mod.asyncio, "sleep", _zero)


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def _api(fragment: str) -> str:
    return f"https://i.instagram.com{fragment}?count=12"


class HomeEndpointTests(unittest.TestCase):
    def test_returns_posts_skipping_ads(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(_api("/api/v1/feed/timeline/"), _load("api_timeline.json"))
            ]
        )
        with _no_sleep_patch():
            posts = _run(home.fetch(page, limit=10))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20"])
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/"])

    def test_limit_caps_results(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(_api("/api/v1/feed/timeline/"), _load("api_timeline.json"))
            ]
        )
        with _no_sleep_patch():
            posts = _run(home.fetch(page, limit=1))
        self.assertEqual(len(posts), 1)


class ProfileEndpointTests(unittest.TestCase):
    def test_fetch_profile(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/users/web_profile_info/"),
                    _load("api_web_profile_info.json"),
                )
            ]
        )
        result = _run(profile.fetch_profile(page, username="carol"))
        assert result is not None
        self.assertEqual(result.username, "carol")
        self.assertEqual(result.follower_count, 4200)
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/"])

    def test_fetch_profile_strips_at_prefix(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/users/web_profile_info/"),
                    _load("api_web_profile_info.json"),
                )
            ]
        )
        _run(profile.fetch_profile(page, username="@carol"))
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/"])

    def test_invalid_username_rejected(self) -> None:
        page = FakePage()
        with self.assertRaises(ValueError):
            _run(profile.fetch_profile(page, username="bad name!"))

    def test_posts_from_embedded_web_profile(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/users/web_profile_info/"),
                    _load("api_web_profile_info.json"),
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(profile.fetch_posts(page, username="carol", limit=10))
        self.assertEqual([p.id for p in posts], ["2001_500", "2002_500"])

    def test_posts_fall_through_to_feed_user(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/users/web_profile_info/"),
                    _load("api_web_profile_info_empty_edges.json"),
                ),
                FakeResponse(
                    _api("/api/v1/feed/user/"), _load("api_feed_user.json")
                ),
            ]
        )
        with _no_sleep_patch():
            posts = _run(profile.fetch_posts(page, username="carol", limit=10))
        # 3002 is a reel — dropped by default.
        self.assertEqual([p.id for p in posts], ["3001_500"])

    def test_posts_include_reels(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(_api("/api/v1/feed/user/"), _load("api_feed_user.json"))
            ]
        )
        with _no_sleep_patch():
            posts = _run(
                profile.fetch_posts(
                    page, username="carol", limit=10, include_reels=True
                )
            )
        self.assertEqual([p.id for p in posts], ["3001_500", "3002_500"])

    def test_posts_dedupe_repeated_payloads(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(_api("/api/v1/feed/user/"), _load("api_feed_user.json")),
                FakeResponse(_api("/api/v1/feed/user/"), _load("api_feed_user.json")),
            ]
        )
        with _no_sleep_patch():
            posts = _run(
                profile.fetch_posts(
                    page, username="carol", limit=10, include_reels=True
                )
            )
        self.assertEqual(len(posts), 2)

    def test_fetch_reels(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(_api("/api/v1/clips/user/"), _load("api_clips_user.json"))
            ]
        )
        with _no_sleep_patch():
            reels = _run(profile.fetch_reels(page, username="carol", limit=10))
        self.assertEqual([r.id for r in reels], ["5001_500", "5002_500"])
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/reels/"])


class PostEndpointTests(unittest.TestCase):
    def test_fetch_by_shortcode(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    "https://www.instagram.com/graphql/query",
                    _load("graphql_post_image.json"),
                )
            ]
        )
        with _no_sleep_patch():
            result = _run(post.fetch(page, id_or_url="PI1"))
        assert result is not None
        self.assertEqual(result.id, "4001_500")
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/p/PI1/"])

    def test_fetch_by_url(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    "https://www.instagram.com/graphql/query",
                    _load("graphql_post_carousel.json"),
                )
            ]
        )
        with _no_sleep_patch():
            result = _run(
                post.fetch(page, id_or_url="https://www.instagram.com/p/PCAR/")
            )
        assert result is not None
        self.assertEqual(result.shortcode, "PCAR")
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/p/PCAR/"])

    def test_reel_url_resolves(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    "https://www.instagram.com/graphql/query",
                    _load("graphql_post_video.json"),
                )
            ]
        )
        with _no_sleep_patch():
            result = _run(
                post.fetch(page, id_or_url="https://www.instagram.com/reel/PVID/")
            )
        assert result is not None
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/p/PVID/"])

    def test_invalid_input_raises(self) -> None:
        page = FakePage()
        with self.assertRaises(ValueError):
            _run(post.fetch(page, id_or_url="bad shortcode!"))

    def test_returns_none_on_missing_payload(self) -> None:
        page = FakePage(responses=[])
        with _no_sleep_patch(), mock.patch.object(
            scraper_mod, "SINGLE_FETCH_TIMEOUT_S", 0.05
        ):
            result = _run(post.fetch(page, id_or_url="PI1"))
        self.assertIsNone(result)


class SavedEndpointTests(unittest.TestCase):
    def test_all_posts_bucket(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/feed/saved/posts/"), _load("api_feed_saved.json")
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(saved.fetch_saved(page, handle="carol", limit=10))
        self.assertEqual([p.id for p in posts], ["6001_70", "6002_71"])
        self.assertEqual(
            page.goto_calls, ["https://www.instagram.com/carol/saved/all-posts/"]
        )

    def test_specific_collection(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/feed/collection/"),
                    _load("api_feed_collection.json"),
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(
                saved.fetch_saved(
                    page, handle="carol", collection_id="17900000000000001", limit=10
                )
            )
        self.assertEqual([p.id for p in posts], ["7001_80", "7002_81"])
        self.assertEqual(
            page.goto_calls,
            ["https://www.instagram.com/carol/saved/17900000000000001/"],
        )

    def test_fetch_collections(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    _api("/api/v1/collections/list/"),
                    _load("api_collections_list.json"),
                )
            ]
        )
        with _no_sleep_patch():
            collections = _run(saved.fetch_collections(page, handle="carol"))
        self.assertEqual(
            [c.name for c in collections], ["All Posts", "Recipes", "Travel"]
        )
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/saved/"])


if __name__ == "__main__":
    unittest.main()
