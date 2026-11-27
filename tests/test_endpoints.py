"""Endpoint tests over a fake in-page client (no browser, no network).

The ``FakeClient`` answers ``graphql``/``rest``/``navigate_html`` from
fixtures and records every call, so each test pins which operations an
endpoint issues, with which variables, and how it turns them into dataclasses.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient, load, text  # noqa: E402

from instagram_sdk.endpoints import home, media, post, profile, saved  # noqa: E402
from instagram_sdk.errors import OperationFailedError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _embed(payload) -> str:
    return f'<script type="application/json">{json.dumps(payload)}</script>'


class SavedTests(unittest.TestCase):
    def test_all_saved_walks_rest_pages_with_max_id(self) -> None:
        ig = FakeClient(rest={"saved_posts": [load("api_feed_saved.json"),
                                              load("api_feed_saved_p2.json")]})
        posts = _run(saved.fetch_saved(ig, handle="ignored", limit=10))
        self.assertEqual([p.id for p in posts], ["6001_70", "6002_71", "6003_72"])
        queries = [c[2]["query"] for c in ig.calls if c[0] == "rest"]
        self.assertEqual(queries, [{"max_id": None}, {"max_id": "NEXTMAX_SAVED"}])

    def test_limit_stops_before_next_page(self) -> None:
        ig = FakeClient(rest={"saved_posts": [load("api_feed_saved.json")]})
        page = _run(saved.fetch_saved_page(ig, limit=1))
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.stopped, "limit")
        self.assertEqual(page.next.skip, 1)

    def test_all_media_collection_id_uses_rest(self) -> None:
        ig = FakeClient(rest={"saved_posts": [load("api_feed_saved_p2.json")]})
        _run(saved.fetch_saved(ig, collection_id="ALL_MEDIA_AUTO_COLLECTION", limit=5))
        self.assertEqual(ig.ops_called(), ["saved_posts"])

    def test_collection_goes_through_graphql(self) -> None:
        ig = FakeClient(graphql={"saved_collection": [load("gql_collection_media.json")]})
        posts = _run(saved.fetch_saved(ig, collection_id="17900000000000001", limit=2))
        self.assertEqual([p.id for p in posts], ["7001_80", "7002_81"])
        self.assertIsNone(posts[0].taken_at)  # this surface carries no taken_at (R3)
        self.assertEqual(ig.calls[0][2], {"collection_id": "17900000000000001", "after": None})

    def test_unknown_collection_is_an_error_not_empty(self) -> None:
        ig = FakeClient(graphql={"saved_collection": [{"data": {"fetch__MediaCollection": None}}]})
        with self.assertRaises(ValueError):
            _run(saved.fetch_saved(ig, collection_id="123", limit=5))

    def test_collections_new_root_drops_audio(self) -> None:
        ig = FakeClient(graphql={"saved_collections": [load("gql_collections_v2.json")]})
        cols = _run(saved.fetch_collections(ig))
        self.assertEqual([c.name for c in cols], ["All posts", "Recipes", "Empty"])
        self.assertEqual(cols[0].cover_image_url, "https://cdn.example.com/allcover.jpg")
        self.assertEqual(ig.calls[0][2], {"after": None})


class HomeTests(unittest.TestCase):
    def test_embedded_first_batch_then_pagination_query(self) -> None:
        page2 = copy.deepcopy(load("gql_timeline.json"))
        conn = page2["data"]["xdt_api__v1__feed__timeline__connection"]
        conn["edges"][0]["node"]["media"]["id"] = "1003_10"
        conn["page_info"] = {"end_cursor": None, "has_next_page": False}
        ig = FakeClient(html={"/": text("home_page.html")},
                        graphql={"home_page": [page2]})
        posts = _run(home.fetch(ig, limit=10))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20", "1003_10"])
        self.assertEqual(ig.ops_called(), ["/", "home_page"])
        self.assertEqual(ig.calls[-1][2], {"after": "TL_CURSOR", "first": 12})

    def test_without_embed_uses_first_page_query(self) -> None:
        ig = FakeClient(graphql={"home_first": [load("gql_timeline.json")]})
        posts = _run(home.fetch(ig, limit=2))
        self.assertEqual([p.id for p in posts], ["1001_10", "1002_20"])
        self.assertEqual(ig.ops_called(), ["/", "home_first"])

    def test_page_of_only_duplicates_ends(self) -> None:
        ig = FakeClient(html={"/": text("home_page.html")},
                        graphql={"home_page": [load("gql_timeline.json")]})
        page = _run(home.fetch_home_page(ig, limit=10))
        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.stopped, "end")


class ProfileTests(unittest.TestCase):
    def test_profile_resolves_id_by_exact_search_match(self) -> None:
        ig = FakeClient(graphql={"search_box": [load("gql_search_users.json")],
                                 "profile_info": [load("gql_profile.json")]})
        result = _run(profile.fetch_profile(ig, username="@carol"))
        self.assertEqual(result.username, "carol")
        self.assertEqual(ig.calls[0][2], {"query": "carol"})
        self.assertEqual(ig.calls[1][2], {"id": "500"})
        # Cached: a second call skips the search.
        ig2_answers = {"profile_info": [load("gql_profile.json")]}
        ig._graphql.update(ig2_answers)
        _run(profile.fetch_profile(ig, username="carol"))
        self.assertEqual(ig.ops_called()[-1], "profile_info")
        self.assertEqual(ig.ops_called().count("search_box"), 1)

    def test_profile_falls_back_to_grid_owner(self) -> None:
        no_match = {"data": {"xdt_api__v1__fbsearch__topsearch_connection": {"users": []}}}
        ig = FakeClient(graphql={"search_box": [no_match],
                                 "profile_posts_first": [load("gql_profile_posts_p1.json")],
                                 "profile_info": [load("gql_profile.json")]})
        result = _run(profile.fetch_profile(ig, username="carol"))
        self.assertEqual(result.pk, "500")
        self.assertEqual(ig.ops_called(), ["search_box", "profile_posts_first", "profile_info"])

    def test_unknown_user_is_none(self) -> None:
        no_match = {"data": {"xdt_api__v1__fbsearch__topsearch_connection": {"users": []}}}
        null_grid = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": None}}
        ig = FakeClient(graphql={"search_box": [no_match], "profile_posts_first": [null_grid]})
        self.assertIsNone(_run(profile.fetch_profile(ig, username="ghost")))

    def test_invalid_username_rejected_before_any_call(self) -> None:
        ig = FakeClient()
        with self.assertRaises(ValueError):
            _run(profile.fetch_profile(ig, username="bad name!"))
        self.assertEqual(ig.calls, [])

    def test_reel_only_page_does_not_stop_the_walk(self) -> None:
        ig = FakeClient(graphql={"profile_posts_first": [load("gql_profile_posts_p1.json")],
                                 "profile_posts_page": [load("gql_profile_posts_p2.json")]})
        posts = _run(profile.fetch_posts(ig, username="carol", limit=5))
        self.assertEqual([p.shortcode for p in posts], ["GP1", "GP2"])
        self.assertEqual(ig.calls[1][2], {"username": "carol", "after": "GRID_P2"})

    def test_page_cap_is_reported_with_a_cursor(self) -> None:
        ig = FakeClient(graphql={"profile_posts_first": [load("gql_profile_posts_p1.json")]})
        page = _run(profile.fetch_posts_page(ig, username="carol", limit=5, max_pages=1))
        self.assertEqual(page.items, [])
        self.assertEqual(page.stopped, "page_cap")
        self.assertEqual(page.next.upstream, "GRID_P2")

    def test_include_reels_fills_views_from_reels_tab(self) -> None:
        grid = copy.deepcopy(load("gql_profile_posts_p2.json"))
        node = grid["data"]["xdt_api__v1__feed__user_timeline_graphql_connection"]["edges"][1]["node"]
        node["code"] = "RLNEW1"
        ig = FakeClient(graphql={"profile_posts_first": [grid],
                                 "profile_reels_first": [load("gql_reels_tab_connection.json")]})
        posts = _run(profile.fetch_posts(ig, username="carol", limit=5, include_reels=True))
        by_code = {p.shortcode: p.view_count for p in posts}
        self.assertEqual(by_code["RLNEW1"], 123456)
        self.assertIsNone(by_code["GP1"])
        self.assertEqual(ig.calls[-1][2], {"id": "500"})

    def test_view_lookup_failure_does_not_fail_the_call(self) -> None:
        grid = load("gql_profile_posts_p2.json")
        ig = FakeClient(graphql={"profile_posts_first": [grid],
                                 "profile_reels_first": [OperationFailedError("x", "boom")]})
        posts = _run(profile.fetch_posts(ig, username="carol", limit=5, include_reels=True))
        self.assertEqual(len(posts), 3)

    def test_fetch_reels_by_id(self) -> None:
        ig = FakeClient(graphql={"search_box": [load("gql_search_users.json")],
                                 "profile_reels_first": [load("gql_reels.json")]})
        reels = _run(profile.fetch_reels(ig, username="carol", limit=2))
        self.assertEqual([r.id for r in reels], ["5001_500", "5002_500"])
        self.assertEqual(ig.calls[-1][2], {"id": "500"})


class PostTests(unittest.TestCase):
    def test_fetch_by_url(self) -> None:
        ig = FakeClient(html={"/p/PI1/": text("post_page.html")})
        result = _run(post.fetch(ig, id_or_url="https://www.instagram.com/reel/PI1/"))
        self.assertEqual(result.id, "4001_500")
        self.assertEqual(ig.ops_called(), ["/p/PI1/"])

    def test_none_when_not_embedded(self) -> None:
        ig = FakeClient(html={"/p/PI1/": "<html>no data</html>"})
        self.assertIsNone(_run(post.fetch(ig, id_or_url="PI1")))

    def test_reel_gets_views_from_author_reels_tab(self) -> None:
        html = text("post_page.html").replace('"code":"PI1"', '"code":"RLNEW1"').replace(
            '"product_type":"feed"', '"product_type":"clips"')
        ig = FakeClient(html={"/p/RLNEW1/": html},
                        graphql={"profile_reels_first": [load("gql_reels_tab_connection.json")]})
        result = _run(post.fetch(ig, id_or_url="RLNEW1"))
        self.assertEqual(result.view_count, 123456)
        # download_media path: no view lookup at all.
        ig2 = FakeClient(html={"/p/RLNEW1/": html})
        code, items = _run(media.resolve_post_media(ig2, id_or_url="RLNEW1"))
        self.assertEqual((code, len(items)), ("RLNEW1", 1))
        self.assertEqual(ig2.ops_called(), ["/p/RLNEW1/"])

    def test_resolve_media_raises_when_missing(self) -> None:
        ig = FakeClient()
        with self.assertRaises(ValueError):
            _run(media.resolve_post_media(ig, id_or_url="ZZZ"))


if __name__ == "__main__":
    unittest.main()
