"""Comments and search endpoints over the fake in-page client, plus their tools.

Fixtures are synthetic (``gql_comments_*``, ``gql_comment_replies``,
``gql_keyword_serp*``), shaped like the captured responses.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient, load  # noqa: E402

from instagram_sdk import ops  # noqa: E402
from instagram_sdk import server as server_mod  # noqa: E402
from instagram_sdk.cursors import CursorState  # noqa: E402
from instagram_sdk.endpoints import comments, search  # noqa: E402
from instagram_sdk.errors import BudgetExceededError  # noqa: E402

MEDIA_ID = "4036967786653960788"  # DgGMNAtNEpU, the pair checked in discovery


def _run(coro):
    return asyncio.run(coro)


class MediaIdTests(unittest.TestCase):
    def test_shortcode_url_and_numeric_id(self) -> None:
        self.assertEqual(comments.media_id_of("DgGMNAtNEpU"), MEDIA_ID)
        self.assertEqual(comments.media_id_of("https://www.instagram.com/reel/DgGMNAtNEpU/"), MEDIA_ID)
        self.assertEqual(comments.media_id_of(MEDIA_ID), MEDIA_ID)
        with self.assertRaises(ValueError):
            comments.media_id_of("not a post!")


class CommentsTests(unittest.TestCase):
    def test_two_pages_dedup_and_variables(self) -> None:
        ig = FakeClient(graphql={"comments_first": [load("gql_comments_p1.json")],
                                 "comments_page": [load("gql_comments_p2.json")]})
        page = _run(comments.fetch_comments_page(ig, id_or_url="DgGMNAtNEpU", limit=10))
        self.assertEqual([c.pk for c in page.items], ["9001", "9002", "9003", "9004", "9005"])
        self.assertEqual(page.stopped, "end")
        self.assertIsNone(page.next)
        first, second = [c[2] for c in ig.calls]
        self.assertEqual(first, {"media_id": MEDIA_ID})
        self.assertEqual(second["media_id"], MEDIA_ID)
        self.assertEqual(json.loads(second["after"])["cached_comments_cursor"], "9003")
        top = page.items[0]
        self.assertEqual((top.text, top.like_count, top.reply_count, top.is_pinned),
                         ("Pinned hello", 12, 2, True))
        self.assertEqual(top.author.username, "carolfan")
        self.assertIsNone(top.replies)
        self.assertTrue(page.items[1].is_edited)

    def test_limit_cuts_mid_page_and_resumes(self) -> None:
        ig = FakeClient(graphql={"comments_first": [load("gql_comments_p1.json")]})
        page = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=2))
        self.assertEqual(page.stopped, "limit")
        self.assertEqual(page.next, CursorState(None, 2))
        ig = FakeClient(graphql={"comments_first": [load("gql_comments_p1.json")]})
        rest = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=1, start=page.next))
        self.assertEqual([c.pk for c in rest.items], ["9003"])

    def test_repeats_across_calls_are_dropped(self) -> None:
        ig = FakeClient(graphql={"comments_first": [load("gql_comments_p1.json")]})
        page = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=3))
        self.assertEqual(page.next.skip, 0)
        ig = FakeClient(graphql={"comments_page": [load("gql_comments_p2.json")]})
        more = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=5,
                                                 start=page.next))
        # Page 2 repeats 9003 from page 1: the cursor carries the ids seen.
        self.assertEqual([c.pk for c in more.items], ["9004", "9005"])

    def test_malformed_cursor_fails_before_any_request(self) -> None:
        ig = FakeClient()
        for upstream in ("raw-instagram-cursor", '{"seen": []}', '{"after": 1}'):
            with self.subTest(upstream=upstream), self.assertRaises(ValueError):
                _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=1,
                                                  start=CursorState(upstream)))
        self.assertEqual(ig.calls, [])

    def test_missing_post_yields_nothing(self) -> None:
        ig = FakeClient(graphql={"comments_first": [None]})
        page = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=5))
        self.assertEqual((page.items, page.stopped), ([], "end"))

    def test_replies_are_a_capped_sample(self) -> None:
        replies = load("gql_comment_replies.json")
        ig = FakeClient(graphql={
            "comments_first": [load("gql_comments_p1.json")],
            "comments_page": [load("gql_comments_p2.json")],
            "comment_replies": [replies] * comments.MAX_REPLY_THREADS,
        })
        page = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=10,
                                                 include_replies=True))
        by_pk = {c.pk: c for c in page.items}
        self.assertEqual([r.pk for r in by_pk["9001"].replies], ["9101", "9102"])
        self.assertEqual(by_pk["9001"].replies[0].parent_pk, "9001")
        self.assertEqual(by_pk["9002"].replies, ())  # no replies: none fetched
        # 9001, 9003, 9004 have replies → 3 threads; 9005 is past the cap.
        self.assertIsNotNone(by_pk["9004"].replies)
        self.assertIsNone(by_pk["9005"].replies)
        asked = [c[2]["comment_id"] for c in ig.calls if c[1] == "comment_replies"]
        self.assertEqual(asked, ["9001", "9003", "9004"])

    def test_replies_stop_quietly_on_budget(self) -> None:
        ig = FakeClient(graphql={
            "comments_first": [load("gql_comments_p1.json")],
            "comment_replies": [BudgetExceededError("cap", retry_after_s=0.0)],
        })
        page = _run(comments.fetch_comments_page(ig, id_or_url=MEDIA_ID, limit=3,
                                                 include_replies=True))
        self.assertEqual(len(page.items), 3)
        self.assertIsNone(page.items[0].replies)
        self.assertIsNone(page.items[2].replies)

    def test_ops_count_in_their_budget_class(self) -> None:
        for name in ("comments_first", "comments_page", "comment_replies"):
            self.assertEqual(ops.TABLE[name].budget_class, "comments")
        for name in ("search_box", "keyword_first", "keyword_page"):
            self.assertEqual(ops.TABLE[name].budget_class, "search")


class SearchUsersTests(unittest.TestCase):
    def test_users_in_order_with_limit(self) -> None:
        ig = FakeClient(graphql={"search_box": [load("gql_search_users.json")]})
        users = _run(search.search_users(ig, query="  @carol ", limit=1))
        self.assertEqual([u.username for u in users], ["carolfan"])
        self.assertEqual(ig.calls[0][2], {"query": "carol"})

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(ValueError):
            search.normalize_query("   ")


class HashtagTests(unittest.TestCase):
    def test_walks_pages_with_one_session(self) -> None:
        ig = FakeClient(graphql={"keyword_first": [load("gql_keyword_serp.json")],
                                 "keyword_page": [load("gql_keyword_serp_p2.json")]})
        page = _run(search.fetch_hashtag_page(ig, tag="#Travel", limit=10))
        self.assertEqual([p.shortcode for p in page.items],
                         ["TAGAAAAAAA1", "TAGAAAAAAA2", "TAGAAAAAAA3", "TAGAAAAAAA4"])
        self.assertEqual(page.stopped, "end")
        first, second = [c[2] for c in ig.calls]
        self.assertEqual(first["query"], "#travel")
        self.assertEqual(second["after"], "SERP_CURSOR_2")
        self.assertEqual(first["session_id"], second["session_id"])
        variables = ops.build_variables(ops.TABLE["keyword_page"], second)
        self.assertEqual(variables["search_session_id"], variables["serp_session_id"])

    def test_resume_keeps_the_session(self) -> None:
        ig = FakeClient(graphql={"keyword_first": [load("gql_keyword_serp.json")]})
        page = _run(search.fetch_hashtag_page(ig, tag="travel", limit=3))
        self.assertEqual(page.stopped, "limit")
        sid = ig.calls[0][2]["session_id"]
        self.assertEqual(json.loads(page.next.upstream), {"sid": sid, "after": "SERP_CURSOR_2"})
        ig = FakeClient(graphql={"keyword_page": [load("gql_keyword_serp_p2.json")]})
        more = _run(search.fetch_hashtag_page(ig, tag="travel", limit=5, start=page.next))
        # No state across calls: an item repeated by Instagram on the next page
        # is only deduplicated within a call.
        self.assertEqual([p.shortcode for p in more.items], ["TAGAAAAAAA3", "TAGAAAAAAA4"])
        self.assertEqual(ig.calls[0][2]["session_id"], sid)

    def test_bad_input_fails_before_any_request(self) -> None:
        ig = FakeClient()
        for tag in ("", "#", "two words", "semi;colon"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                _run(search.fetch_hashtag_page(ig, tag=tag, limit=1))
        with self.assertRaises(ValueError):
            _run(search.fetch_hashtag_page(ig, tag="ok", limit=1, start=CursorState("nope")))
        self.assertEqual(ig.calls, [])
        self.assertEqual(search.normalize_tag("#Café_2"), "café_2")


class ToolTests(unittest.TestCase):
    def _runtime(self):
        from test_server import FakeRuntime  # same double as the other tool tests

        return FakeRuntime()

    def test_comments_cursor_round_trip_and_query_binding(self) -> None:
        runtime = self._runtime()

        def fake_page(**kwargs):
            from instagram_sdk.cursors import PageResult

            return PageResult([comments.parse_comment({"pk": "1", "text": "hi"})],
                              CursorState('{"cached_comments_cursor":"x"}', 0), "limit")

        fetch = mock.AsyncMock(side_effect=lambda ig, **kw: fake_page(**kw))
        with mock.patch.object(server_mod, "get_runtime", return_value=runtime), \
                mock.patch("instagram_sdk.server.comments.fetch_comments_page", new=fetch):
            out = _run(server_mod.get_post_comments("DgGMNAtNEpU", limit=1))
            self.assertEqual(out["items"][0]["text"], "hi")
            self.assertEqual(fetch.call_args.kwargs["id_or_url"], MEDIA_ID)
            cursor = out["next_cursor"]
            # Same post by URL → same query: accepted.
            _run(server_mod.get_post_comments(
                "https://www.instagram.com/p/DgGMNAtNEpU/", cursor=cursor))
            self.assertEqual(fetch.call_args.kwargs["start"],
                             CursorState('{"cached_comments_cursor":"x"}', 0))
            # Replies on or off: same comments, same cursor.
            _run(server_mod.get_post_comments("DgGMNAtNEpU", cursor=cursor,
                                              include_replies=True))
            self.assertTrue(fetch.call_args.kwargs["include_replies"])
            calls = fetch.call_count
            with self.assertRaises(ValueError):
                _run(server_mod.get_post_comments("DgGMNAtNEpA", cursor=cursor))
            with self.assertRaises(ValueError):
                _run(server_mod.search_hashtag("travel", cursor=cursor))
            self.assertEqual(fetch.call_count, calls)

    def test_search_tools_validate_and_serialize(self) -> None:
        runtime = self._runtime()
        users = mock.AsyncMock(return_value=[])
        with mock.patch.object(server_mod, "get_runtime", return_value=runtime), \
                mock.patch("instagram_sdk.server.search.search_users", new=users):
            self.assertEqual(_run(server_mod.search_users("natgeo", limit=500)), [])
            self.assertEqual(users.call_args.kwargs["limit"], server_mod.SEARCH_MAX_LIMIT)
            with self.assertRaises(ValueError):
                _run(server_mod.search_users("  "))
            with self.assertRaises(ValueError):
                _run(server_mod.search_hashtag("two words"))
        self.assertEqual(users.call_count, 1)


if __name__ == "__main__":
    unittest.main()
