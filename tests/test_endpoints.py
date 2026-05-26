"""Endpoint integration tests with a faked Playwright Page.

A ``FakePage`` records response handlers and fires synthetic ``FakeResponse``
objects through them on ``goto``. GraphQL responses carry an
``fb_api_req_friendly_name`` (in a fake request body) exactly as Instagram's
do; REST responses are matched by URL. The endpoint code runs end-to-end
except for real browser IO and scrolling sleeps (``asyncio.sleep`` is patched
to a no-op).

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


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRequest:
    def __init__(self, friendly: str | None) -> None:
        self.method = "POST" if friendly else "GET"
        self.post_data = (
            f"av=1&fb_api_req_friendly_name={friendly}&variables=%7B%7D"
            if friendly
            else None
        )


class FakeResponse:
    """Mimics a playwright.async_api.Response for the response handler."""

    def __init__(
        self, url: str, payload: dict[str, Any], *, friendly: str | None = None
    ) -> None:
        self.url = url
        self._payload = payload
        self.request = FakeRequest(friendly)

    async def json(self) -> dict[str, Any]:
        return self._payload


class FakePage:
    """Minimal Page double — fires synthetic responses on ``goto``."""

    def __init__(
        self,
        *,
        url: str = "https://www.instagram.com/",
        responses: list[FakeResponse] | None = None,
        html: str = "<html></html>",
    ) -> None:
        self.url = url
        self._responses = responses or []
        self._html = html
        self._listeners: dict[str, list[Callable[..., Awaitable[None]]]] = {}
        self.scroll_calls = 0
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
        self.scroll_calls += 1

    async def content(self) -> str:
        return self._html

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


GRAPHQL_URL = "https://www.instagram.com/api/graphql"
TIMELINE_FN = "PolarisFeedRootPaginationCachedQuery_subscribe"


def _embed_html(payload: dict[str, Any]) -> str:
    """Wrap a payload in a script tag the way Instagram server-renders data."""
    return f'<script type="application/json">{json.dumps(payload)}</script>'


class HomeEndpointTests(unittest.TestCase):
    def test_returns_posts_skipping_ads(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL, _load("gql_timeline.json"), friendly=TIMELINE_FN
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(home.fetch(page, limit=10))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20"])
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/"])

    def test_limit_caps_results(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL, _load("gql_timeline.json"), friendly=TIMELINE_FN
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(home.fetch(page, limit=1))
        self.assertEqual(len(posts), 1)

    def test_seeds_from_embedded_html(self) -> None:
        # No pagination responses — posts come only from the embedded HTML.
        page = FakePage(html=_embed_html(_load("gql_timeline.json")))
        with _no_sleep_patch():
            posts = _run(home.fetch(page, limit=10))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20"])

    def test_embedded_and_paginated_are_deduped(self) -> None:
        page = FakePage(
            html=_embed_html(_load("gql_timeline.json")),
            responses=[
                FakeResponse(
                    GRAPHQL_URL, _load("gql_timeline.json"), friendly=TIMELINE_FN
                )
            ],
        )
        with _no_sleep_patch():
            posts = _run(home.fetch(page, limit=10))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20"])


class ProfileEndpointTests(unittest.TestCase):
    def _profile_page(self) -> FakePage:
        return FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL,
                    _load("gql_profile.json"),
                    friendly="PolarisProfilePageContentQuery",
                )
            ]
        )

    def test_fetch_profile(self) -> None:
        page = self._profile_page()
        result = _run(profile.fetch_profile(page, username="carol"))
        assert result is not None
        self.assertEqual(result.username, "carol")
        self.assertEqual(result.follower_count, 4200)
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/"])

    def test_fetch_profile_strips_at_prefix(self) -> None:
        page = self._profile_page()
        _run(profile.fetch_profile(page, username="@carol"))
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/"])

    def test_invalid_username_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _run(profile.fetch_profile(FakePage(), username="bad name!"))

    def test_posts_exclude_reels_by_default(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL,
                    _load("gql_profile_posts.json"),
                    friendly="PolarisProfilePostsQuery",
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(profile.fetch_posts(page, username="carol", limit=10))
        self.assertEqual([p.id for p in posts], ["2001_500"])
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/"])

    def test_posts_include_reels(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL,
                    _load("gql_profile_posts.json"),
                    friendly="PolarisProfilePostsQuery",
                )
            ]
        )
        with _no_sleep_patch():
            posts = _run(
                profile.fetch_posts(
                    page, username="carol", limit=10, include_reels=True
                )
            )
        self.assertEqual([p.id for p in posts], ["2001_500", "2002_500"])

    def test_fetch_reels(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL,
                    _load("gql_reels.json"),
                    friendly="PolarisProfileReelsTabContentQuery",
                )
            ]
        )
        with _no_sleep_patch():
            reels = _run(profile.fetch_reels(page, username="carol", limit=10))
        self.assertEqual([r.id for r in reels], ["5001_500", "5002_500"])
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/carol/reels/"])


class PostEndpointTests(unittest.TestCase):
    def test_fetch_by_shortcode(self) -> None:
        page = FakePage(html=_text("post_page.html"))
        with _no_sleep_patch():
            result = _run(post.fetch(page, id_or_url="PI1"))
        assert result is not None
        self.assertEqual(result.id, "4001_500")
        self.assertEqual(result.shortcode, "PI1")
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/p/PI1/"])

    def test_fetch_by_url(self) -> None:
        page = FakePage(html=_text("post_page.html"))
        with _no_sleep_patch():
            result = _run(
                post.fetch(page, id_or_url="https://www.instagram.com/reel/PI1/")
            )
        assert result is not None
        self.assertEqual(page.goto_calls, ["https://www.instagram.com/p/PI1/"])

    def test_invalid_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            _run(post.fetch(FakePage(), id_or_url="bad shortcode!"))

    def test_returns_none_when_not_embedded(self) -> None:
        page = FakePage(html="<html><body>no data</body></html>")
        with _no_sleep_patch():
            result = _run(post.fetch(page, id_or_url="PI1"))
        self.assertIsNone(result)


class SavedEndpointTests(unittest.TestCase):
    def test_all_posts_bucket(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    "https://i.instagram.com/api/v1/feed/saved/posts/?count=12",
                    _load("api_feed_saved.json"),
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
                    "https://i.instagram.com/api/v1/feed/collection/17900000000000001/posts/",
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
            [
                "https://www.instagram.com/carol/saved/"
                "17900000000000001/17900000000000001/"
            ],
        )

    def test_fetch_collections(self) -> None:
        page = FakePage(
            responses=[
                FakeResponse(
                    GRAPHQL_URL,
                    _load("gql_collections.json"),
                    friendly="PolarisProfileSavedTabContentQuery",
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
