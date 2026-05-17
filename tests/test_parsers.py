"""Unit tests for instagram_sdk.parsers.

Each test loads a captured/synthesized fixture from ``tests/fixtures/*.json``
and asserts the extractor returns the expected dataclasses. Pure CPU — no
Playwright, no network.

Run with: ``venv/bin/python -m unittest tests.test_parsers -v``
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import parsers  # noqa: E402
from instagram_sdk.models import Collection, Post, Profile, Reel  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ExtractHomeTests(unittest.TestCase):
    def test_skips_ads_and_non_media_items(self) -> None:
        posts = parsers.extract_home(_load("api_timeline.json"))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20"])

    def test_image_post_fields(self) -> None:
        post = parsers.extract_home(_load("api_timeline.json"))[0]
        self.assertIsInstance(post, Post)
        self.assertEqual(post.shortcode, "AAA1")
        self.assertEqual(post.url, "https://www.instagram.com/p/AAA1/")
        self.assertEqual(post.caption, "Post one")
        self.assertEqual(post.like_count, 10)
        self.assertEqual(post.comment_count, 2)
        self.assertEqual(post.media_type, 1)
        self.assertEqual(len(post.media), 1)
        # Largest candidate wins.
        self.assertEqual(post.media[0].image_url, "https://cdn.example.com/1001_big.jpg")
        self.assertEqual(post.media[0].width, 1080)
        assert post.author is not None
        self.assertEqual(post.author.username, "alice")

    def test_video_post_has_video_url(self) -> None:
        post = parsers.extract_home(_load("api_timeline.json"))[1]
        self.assertEqual(post.media_type, 2)
        self.assertEqual(post.media[0].video_url, "https://cdn.example.com/1002.mp4")
        self.assertEqual(post.media[0].media_type, 2)
        self.assertEqual(post.view_count, 500)

    def test_handles_missing_root(self) -> None:
        self.assertEqual(parsers.extract_home({}), [])


class ExtractProfileTests(unittest.TestCase):
    def test_full_profile(self) -> None:
        profile = parsers.extract_profile(_load("api_web_profile_info.json"))
        assert profile is not None
        self.assertIsInstance(profile, Profile)
        self.assertEqual(profile.pk, "500")
        self.assertEqual(profile.username, "carol")
        self.assertEqual(profile.full_name, "Carol Doe")
        self.assertEqual(profile.biography, "photographer in madrid")
        self.assertEqual(profile.follower_count, 4200)
        self.assertEqual(profile.following_count, 311)
        self.assertEqual(profile.post_count, 87)
        self.assertTrue(profile.is_verified)
        self.assertFalse(profile.is_private)
        self.assertEqual(
            profile.bio_links,
            ("https://carol.example.com", "https://shop.example.com"),
        )
        # HD picture preferred when present.
        self.assertEqual(profile.profile_pic_url, "https://cdn.example.com/carol_hd.jpg")

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(parsers.extract_profile({"data": {}}))
        self.assertIsNone(parsers.extract_profile({}))


class ExtractProfilePostsTests(unittest.TestCase):
    def test_embedded_web_profile_edges(self) -> None:
        posts = parsers.extract_profile_posts(_load("api_web_profile_info.json"))
        self.assertEqual([p.id for p in posts], ["2001_500", "2002_500"])
        # The sidecar carousel flattens to two leaf media items.
        self.assertEqual(posts[1].media_type, 8)
        self.assertEqual(len(posts[1].media), 2)
        self.assertEqual(posts[1].media[1].media_type, 2)
        self.assertEqual(posts[1].media[1].video_url, "https://cdn.example.com/2002b.mp4")

    def test_empty_edges_yield_nothing(self) -> None:
        posts = parsers.extract_profile_posts(
            _load("api_web_profile_info_empty_edges.json")
        )
        self.assertEqual(posts, [])

    def test_feed_user_items(self) -> None:
        posts = parsers.extract_profile_posts(_load("api_feed_user.json"))
        self.assertEqual([p.id for p in posts], ["3001_500", "3002_500"])
        self.assertEqual(posts[1].product_type, "clips")


class ExtractPostDetailTests(unittest.TestCase):
    def test_image_post(self) -> None:
        post = parsers.extract_post_detail(_load("graphql_post_image.json"))
        assert post is not None
        self.assertEqual(post.id, "4001_500")
        self.assertEqual(post.shortcode, "PI1")
        self.assertEqual(post.media_type, 1)
        self.assertEqual(post.like_count, 99)
        self.assertEqual(post.comment_count, 4)
        self.assertEqual(post.caption, "single image")
        self.assertEqual(post.location, "Madrid")
        assert post.author is not None
        self.assertEqual(post.author.username, "carol")

    def test_carousel_post(self) -> None:
        post = parsers.extract_post_detail(_load("graphql_post_carousel.json"))
        assert post is not None
        self.assertEqual(post.media_type, 8)
        self.assertEqual(len(post.media), 2)
        self.assertEqual(post.media[0].media_type, 1)
        self.assertEqual(post.media[1].media_type, 2)
        self.assertEqual(post.like_count, 150)

    def test_video_post(self) -> None:
        post = parsers.extract_post_detail(_load("graphql_post_video.json"))
        assert post is not None
        self.assertEqual(post.media_type, 2)
        self.assertEqual(post.view_count, 1234)
        self.assertEqual(post.media[0].video_url, "https://cdn.example.com/4003.mp4")

    def test_rejects_non_post_payload(self) -> None:
        self.assertIsNone(parsers.extract_post_detail({}))
        self.assertIsNone(parsers.extract_post_detail({"data": {}}))
        self.assertIsNone(parsers.extract_post_detail({"data": {"xdt_other": {}}}))


class ExtractReelsTests(unittest.TestCase):
    def test_extracts_reels(self) -> None:
        reels = parsers.extract_reels(_load("api_clips_user.json"))
        self.assertEqual([r.id for r in reels], ["5001_500", "5002_500"])
        first = reels[0]
        self.assertIsInstance(first, Reel)
        self.assertEqual(first.play_count, 9999)
        self.assertEqual(first.video_url, "https://cdn.example.com/5001.mp4")
        self.assertEqual(first.thumbnail_url, "https://cdn.example.com/5001_thumb.jpg")
        self.assertEqual(first.like_count, 200)

    def test_empty_payload(self) -> None:
        self.assertEqual(parsers.extract_reels({}), [])


class ExtractSavedTests(unittest.TestCase):
    def test_saved_posts_including_carousel(self) -> None:
        posts = parsers.extract_saved(_load("api_feed_saved.json"))
        self.assertEqual([p.id for p in posts], ["6001_70", "6002_71"])
        self.assertEqual(posts[1].media_type, 8)
        self.assertEqual(len(posts[1].media), 2)

    def test_collection_feed(self) -> None:
        posts = parsers.extract_saved(_load("api_feed_collection.json"))
        self.assertEqual([p.id for p in posts], ["7001_80", "7002_81"])


class ExtractCollectionsTests(unittest.TestCase):
    def test_extracts_collections(self) -> None:
        collections = parsers.extract_collections(_load("api_collections_list.json"))
        self.assertEqual(
            [c.name for c in collections], ["All Posts", "Recipes", "Travel"]
        )
        self.assertIsInstance(collections[0], Collection)
        self.assertEqual(collections[0].collection_id, "ALL_MEDIA_AUTO_COLLECTION")
        self.assertEqual(collections[0].media_count, 120)
        self.assertIsNone(collections[0].cover_image_url)
        self.assertEqual(
            collections[1].cover_image_url, "https://cdn.example.com/recipes_cover.jpg"
        )

    def test_empty_payload(self) -> None:
        self.assertEqual(parsers.extract_collections({}), [])


class ParsePostToleranceTests(unittest.TestCase):
    def test_none_and_garbage_return_none(self) -> None:
        self.assertIsNone(parsers.parse_post(None))
        self.assertIsNone(parsers.parse_post({}))
        self.assertIsNone(parsers.parse_post({"unrelated": "shape"}))

    def test_parse_reel_none(self) -> None:
        self.assertIsNone(parsers.parse_reel(None))

    def test_parse_collection_requires_id(self) -> None:
        self.assertIsNone(parsers.parse_collection({"collection_name": "x"}))
        self.assertIsNone(parsers.parse_collection({}))


if __name__ == "__main__":
    unittest.main()
