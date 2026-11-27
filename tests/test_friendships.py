"""Followers / following: endpoint, pacing and tool, over fakes (no network).

Every payload here is synthetic (``user_<n>``); no real accounts.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient  # noqa: E402

from instagram_sdk import ops  # noqa: E402
from instagram_sdk.cursors import CursorState  # noqa: E402
from instagram_sdk.endpoints import friendships  # noqa: E402
from instagram_sdk.endpoints.friendships import PrivateAccountError  # noqa: E402
from instagram_sdk.pacer import FOLLOW_MIN_GAP_S, Pacer  # noqa: E402

TARGET_PK = "900"
VIEWER_PK = "1"


def _run(coro):
    return asyncio.run(coro)


def _users(start: int, n: int) -> list[dict]:
    return [
        {"pk": str(i), "pk_id": str(i), "id": str(i), "username": f"user_{i}",
         "full_name": f"User {i}", "is_private": i % 2 == 0, "is_verified": False,
         "profile_pic_url": f"https://cdn.example.com/{i}.jpg"}
        for i in range(start, start + n)
    ]


def _page(start: int, n: int = 12, nxt: str | None = None) -> dict:
    return {"users": _users(start, n), "big_list": True, "page_size": 12,
            "next_max_id": nxt, "has_more": nxt is not None, "status": "ok"}


def _search(username: str = "target", pk: str = TARGET_PK) -> dict:
    return {"data": {"xdt_api__v1__fbsearch__topsearch_connection": {
        "users": [{"user": {"pk": pk, "username": username}}]}}}


def _profile(private: bool, following: bool = False) -> dict:
    return {"data": {"user": {"pk": TARGET_PK, "username": "target", "is_private": private,
                              "friendship_status": {"following": following}}}}


def _client(*, rest, graphql=None, viewer=VIEWER_PK) -> FakeClient:
    ig = FakeClient(rest=rest, graphql={"search_box": [_search()], **(graphql or {})})

    async def viewer_id():
        return viewer

    ig.viewer_id = viewer_id
    return ig


class OpsTests(unittest.TestCase):
    def test_ops_registered_with_follow_budget(self) -> None:
        for name in ("followers", "following"):
            op = ops.get(name)
            self.assertEqual(op.budget_class, "follow")
            self.assertEqual(op.query["count"], "12")
            self.assertEqual((op.items_key, op.more_key), ("users", "has_more"))
        self.assertEqual(ops.get("followers").query["search_surface"], "follow_list_page")


class FollowersTests(unittest.TestCase):
    def test_two_pages_by_opaque_cursor_without_overlap(self) -> None:
        ig = _client(rest={"followers": [_page(100, nxt="AQH_opaque"), _page(112, nxt="AQH_2")]},
                     graphql={"profile_info": [_profile(private=False)]})
        page = _run(friendships.fetch_follow_page(ig, "followers", username="@Target", limit=24))
        self.assertEqual([u.username for u in page.items],
                         [f"user_{i}" for i in range(100, 124)])
        self.assertEqual(page.next, CursorState("AQH_2", 0))
        self.assertEqual(page.stopped, "limit")
        rests = [c for c in ig.calls if c[0] == "rest"]
        self.assertEqual(rests[0][2], {"path_params": {"user_id": TARGET_PK},
                                       "query": {"max_id": None}})
        self.assertEqual(rests[1][2]["query"], {"max_id": "AQH_opaque"})
        self.assertTrue(page.items[0].full_name and page.items[0].profile_pic_url)

    def test_limit_mid_page_resumes_with_skip(self) -> None:
        ig = _client(rest={"followers": [_page(100, nxt="AQH_x"), _page(100, nxt="AQH_x")]},
                     graphql={"profile_info": [_profile(private=False)]})
        first = _run(friendships.fetch_follow_page(ig, "followers", username="target", limit=5))
        self.assertEqual(first.next, CursorState(None, 5))
        again = _run(friendships.fetch_follow_page(ig, "followers", username="target",
                                                   limit=5, start=first.next))
        self.assertEqual([u.pk for u in again.items], [str(i) for i in range(105, 110)])
        self.assertNotIn("profile_info", ig.ops_called()[2:])  # no re-check on a cursor

    def test_own_account_skips_visibility_check(self) -> None:
        ig = _client(rest={"followers": [_page(100)]}, viewer=TARGET_PK)
        page = _run(friendships.fetch_follow_page(ig, "followers", username="target", limit=50))
        self.assertEqual(page.stopped, "end")
        self.assertIsNone(page.next)
        self.assertEqual(ig.ops_called(), ["search_box", "followers"])

    def test_handle_alone_does_not_skip_the_check(self) -> None:
        # Only viewer_id() proves it is the own account; a different pk is checked.
        ig = _client(rest={}, graphql={"profile_info": [_profile(private=True)]})
        ig.account = "target"
        with self.assertRaises(PrivateAccountError):
            _run(friendships.fetch_follow_page(ig, "following", username="target", limit=12))

    def test_private_not_followed_fails_without_list_request(self) -> None:
        ig = _client(rest={}, graphql={"profile_info": [_profile(private=True)]})
        with self.assertRaises(PrivateAccountError) as ctx:
            _run(friendships.fetch_follow_page(ig, "followers", username="target", limit=12))
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertIn("private", str(ctx.exception))
        self.assertNotIn("followers", ig.ops_called())

    def test_private_followed_is_listed(self) -> None:
        ig = _client(rest={"following": [_page(1, n=3)]},
                     graphql={"profile_info": [_profile(private=True, following=True)]})
        page = _run(friendships.fetch_follow_page(ig, "following", username="target", limit=12))
        self.assertEqual(len(page.items), 3)

    def test_unknown_user_is_an_error(self) -> None:
        ig = FakeClient(graphql={"search_box": [{"data": {
            "xdt_api__v1__fbsearch__topsearch_connection": {"users": []}}}],
            "profile_posts_first": [None]})
        with self.assertRaises(ValueError):
            _run(friendships.fetch_follow_page(ig, "followers", username="ghost", limit=12))

    def test_page_cap_is_five(self) -> None:
        pages = [_page(i * 12, nxt=f"c{i}") for i in range(6)]
        ig = _client(rest={"followers": pages}, viewer=TARGET_PK)
        page = _run(friendships.fetch_follow_page(ig, "followers", username="target",
                                                  limit=None, max_pages=50))
        self.assertEqual(page.stopped, "page_cap")
        self.assertEqual(len(page.items), 60)
        self.assertEqual(page.next, CursorState("c4", 0))


class FollowingTests(unittest.TestCase):
    def test_numeric_offset_cursor(self) -> None:
        ig = _client(rest={"following": [_page(1, nxt="12"), _page(13, nxt="24")]},
                     viewer=TARGET_PK)
        first = _run(friendships.fetch_follow_page(ig, "following", username="target", limit=12))
        self.assertEqual(first.next, CursorState("12", 0))
        second = _run(friendships.fetch_follow_page(ig, "following", username="target",
                                                    limit=12, start=first.next))
        self.assertEqual(ig.calls[-1][2]["query"], {"max_id": "12"})
        self.assertFalse({u.pk for u in first.items} & {u.pk for u in second.items})


class FollowPacingTests(unittest.TestCase):
    def test_page_pause_and_min_gap_between_calls(self) -> None:
        now = [1000.0]
        slept: list[float] = []

        async def sleep(s):
            slept.append(round(s, 3))
            now[0] += s

        pacer = Pacer(sleep=sleep, clock=lambda: now[0], uniform=lambda a, b: a)

        async def go():
            pacer.new_call()
            await pacer.before_request("follow")   # first: pre pause 1.5
            await pacer.before_request("follow")   # next page: 4.0 (follow pause)
            now[0] += 10
            pacer.new_call()
            await pacer.before_request("follow")   # 30 s gap: 30 - 10 = 20
            pacer.new_call()
            await pacer.before_request("general")  # other classes unaffected: 1.5

        _run(go())
        self.assertEqual(slept, [1.5, 4.0, FOLLOW_MIN_GAP_S - 10, 1.5])


class ToolTests(unittest.TestCase):
    def test_cursor_round_trip_and_isolation(self) -> None:
        import instagram_sdk.server as server_mod
        from instagram_sdk.cursors import PageResult
        from instagram_sdk.models import User

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_server import FakeRuntime

        user = User("5", "user_5", None, False, False, None)
        fetch = mock.AsyncMock(return_value=PageResult([user], CursorState("12", 0), "limit"))
        with mock.patch.object(server_mod, "get_runtime", return_value=FakeRuntime()), \
                mock.patch("instagram_sdk.server.friendships.fetch_follow_page", new=fetch):
            out = _run(server_mod.get_following(username="@Target", limit=1))
            self.assertEqual(out["items"][0]["username"], "user_5")
            self.assertEqual(fetch.call_args.args[1], "following")
            _run(server_mod.get_following(username="target", cursor=out["next_cursor"]))
            self.assertEqual(fetch.call_args.kwargs["start"], CursorState("12", 0))
            calls = fetch.call_count
            with self.assertRaises(ValueError):  # a following cursor is not a followers one
                _run(server_mod.get_followers(username="target", cursor=out["next_cursor"]))
            self.assertEqual(fetch.call_count, calls)
            _run(server_mod.get_followers(username="target", limit=500))
            self.assertEqual(fetch.call_args.kwargs["limit"], 50)


if __name__ == "__main__":
    unittest.main()
