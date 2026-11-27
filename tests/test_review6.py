"""Regression tests for the PR #6 review (one block per finding, no network)."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient, load, text  # noqa: E402
from test_multiaccount import _Base as _MultiBase  # noqa: E402
from test_multiaccount import _no_pauses  # noqa: E402

from instagram_sdk.accounts import Account, AccountStateStore  # noqa: E402
from instagram_sdk.endpoints import home, post, profile, saved  # noqa: E402
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    BrowserUnavailableError,
    BudgetExceededError,
    StateUnavailableError,
)
from instagram_sdk.guard import BlockSignal  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _budget() -> BudgetExceededError:
    return BudgetExceededError("request budget reached", retry_after_s=60.0)


def _session(directory: Path, handle: str = "carol") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{handle}_instagram_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "S"},
                                            {"name": "ds_user_id", "value": "9"}]}))
    return path


class LibraryBudgetTests(unittest.TestCase):
    """1. List-returning library calls never truncate silently on the budget."""

    def test_fetch_saved_raises_with_partial(self) -> None:
        ig = FakeClient(rest={"saved_posts": [load("api_feed_saved.json"), _budget()]})
        with self.assertRaises(BudgetExceededError) as ctx:
            _run(saved.fetch_saved(ig, handle="carol", collection_id=None, limit=40))
        self.assertEqual([p.id for p in ctx.exception.partial], ["6001_70", "6002_71"])
        self.assertEqual(ctx.exception.retry_after_s, 60.0)
        self.assertEqual(ctx.exception.next.upstream, "NEXTMAX_SAVED")

    def test_tools_still_get_the_partial_page(self) -> None:
        ig = FakeClient(rest={"saved_posts": [load("api_feed_saved.json"), _budget()]})
        page = _run(saved.fetch_saved_page(ig, limit=40))
        self.assertEqual(page.stopped, "budget")
        self.assertEqual(len(page.items), 2)

    def test_fetch_posts_reels_and_home_raise(self) -> None:
        reels = copy.deepcopy(load("gql_reels.json"))
        reels["data"]["xdt_api__v1__clips__user__connection_v2"]["page_info"] = {
            "has_next_page": True, "end_cursor": "R2"}
        cases = {
            "posts": (FakeClient(graphql={
                "profile_posts_first": [load("gql_profile_posts_p1.json")],
                "profile_posts_page": [_budget()]}),
                lambda ig: profile.fetch_posts(ig, username="carol", limit=10)),
            "reels": (FakeClient(graphql={
                "search_box": [load("gql_search_users.json")],
                "profile_reels_first": [reels], "profile_reels_page": [_budget()]}),
                lambda ig: profile.fetch_reels(ig, username="carol", limit=10)),
            "home": (FakeClient(html={"/": text("home_page.html")},
                                graphql={"home_page": [_budget()]}),
                     lambda ig: home.fetch(ig, limit=10)),
        }
        expected = {"posts": 0, "reels": 2, "home": 2}
        for name, (ig, call) in cases.items():
            with self.subTest(name), self.assertRaises(BudgetExceededError) as ctx:
                _run(call(ig))
            self.assertEqual(len(ctx.exception.partial), expected[name], name)


class ReelHydrationTests(unittest.TestCase):
    """Mitigation: hydrating reels with post.fetch does not re-walk the tab."""

    def _reel_html(self, code: str) -> str:
        return text("post_page.html").replace('"code":"PI1"', f'"code":"{code}"').replace(
            '"product_type":"feed"', '"product_type":"clips"')

    def test_reels_listed_first_cost_no_view_lookup(self) -> None:
        ig = FakeClient(graphql={"search_box": [load("gql_search_users.json")],
                                 "profile_reels_first": [load("gql_reels.json")]},
                        html={"/p/RL1/": self._reel_html("RL1")})
        reels = _run(profile.fetch_reels(ig, username="carol", limit=2))
        full = _run(post.fetch(ig, id_or_url=reels[0].shortcode))
        self.assertEqual(full.view_count, reels[0].play_count)
        self.assertEqual(ig.ops_called().count("profile_reels_first"), 1)

    def test_a_miss_does_not_walk_the_tab_again(self) -> None:
        ig = FakeClient(graphql={"profile_reels_first": [load("gql_reels_tab_connection.json")]},
                        html={"/p/OLD1/": self._reel_html("OLD1"),
                              "/p/OLD2/": self._reel_html("OLD2")})
        self.assertIsNone(_run(post.fetch(ig, id_or_url="OLD1")).view_count)
        self.assertIsNone(_run(post.fetch(ig, id_or_url="OLD2")).view_count)
        ops = ig.ops_called()
        self.assertEqual(ops.count("profile_reels_first"), 1)
        self.assertEqual(ops[-1], "/p/OLD2/")  # nothing after the second post page
        self.assertEqual(ops.index("/p/OLD2/"), len(ops) - 1)


class FailClosedStateTests(unittest.TestCase):
    """2. Unreadable state never reads as "not blocked"."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.carol = Account.from_session(_session(self.dir / "sessions"))
        self.store = AccountStateStore(self.dir / "state")
        self.store.record(self.carol, BlockSignal("rate_limited", "429"))
        self.path = self.dir / "state" / "accounts" / "carol.json"

    def test_corrupt_account_or_global_file_refuses(self) -> None:
        dave = Account.from_session(_session(self.dir / "sessions", "dave"))
        AccountStateStore(self.dir / "state").save("dave", {"status": "ok", "block": None})
        for target, who in ((self.path, self.carol),
                            (self.dir / "state" / "global.json", dave)):
            with self.subTest(target=target.name):
                saved_text = target.read_text()
                target.write_text("{ truncated")
                with self.assertRaises(StateUnavailableError):
                    AccountStateStore(self.dir / "state").check(who)
                target.write_text(saved_text)

    def test_unreadable_file_refuses(self) -> None:
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions and a non-root user")
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        with self.assertRaises(StateUnavailableError):
            AccountStateStore(self.dir / "state").check(self.carol)

    def test_summary_reports_and_clear_repairs(self) -> None:
        self.path.write_text("[]")
        store = AccountStateStore(self.dir / "state")
        self.assertEqual(store.summary(self.carol)["state"], "unknown")
        self.assertTrue(store.clear("carol"))
        store.check(self.carol)


class FailClosedRuntimeTests(_MultiBase):
    """2. A state dir that can't be written stops calls before any request."""

    def _readonly_state(self):
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions and a non-root user")
        state = self.dir / "ro_state"
        state.mkdir()
        os.chmod(state, 0o555)
        self.addCleanup(os.chmod, state, 0o755)
        return replace(self.settings, state_dir=state)

    def test_refused_without_opt_in(self) -> None:
        runtime, pw = self.runtime(self._readonly_state())
        ran = []
        with self.assertRaises(StateUnavailableError):
            _run(runtime.run("carol", lambda ig: ran.append(1) or asyncio.sleep(0)))
        self.assertEqual((ran, pw.launched), ([], []))
        self.assertFalse(runtime._lock.locked())

    def test_runs_with_explicit_opt_in(self) -> None:
        settings = replace(self._readonly_state(), allow_degraded_state=True)
        runtime, _ = self.runtime(settings)
        _no_pauses(runtime)
        self.assertEqual(_run(runtime.run("carol", lambda ig: asyncio.sleep(0, "ok"))), "ok")

    def test_accounts_dir_not_writable_refuses(self) -> None:
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions and a non-root user")
        accounts = self.settings.state_dir / "accounts"
        accounts.mkdir(parents=True)
        os.chmod(accounts, 0o555)
        self.addCleanup(os.chmod, accounts, 0o755)
        runtime, pw = self.runtime()
        with self.assertRaises(StateUnavailableError):
            _run(runtime.run("carol", lambda ig: asyncio.sleep(0)))
        self.assertEqual(pw.launched, [])


class ResolvedFingerprintTests(unittest.TestCase):
    """3. A symlink or a non-normalised path does not skip a block."""

    def test_same_file_other_spelling_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            sess = _session(d / "sessions")
            store = AccountStateStore(d / "state")
            store.record(Account.from_session(sess), BlockSignal("challenge", "/challenge/"))
            spellings = [d / "sessions" / ".." / "sessions" / sess.name]
            if hasattr(os, "symlink"):
                try:
                    (d / "alias").symlink_to(d / "sessions")
                    spellings.append(d / "alias" / sess.name)
                except OSError:
                    pass
            for path in spellings:
                with self.subTest(path=str(path)), self.assertRaises(AccountBlockedError):
                    store.check(Account.from_session(path))
            block = json.loads((d / "state" / "accounts" / "carol.json").read_text())["block"]
            self.assertEqual(block["user_id"], "9")  # ready to index by ds_user_id


class RetryKeepsCapTests(_MultiBase):
    """4. The retry after a Chromium crash does not get a fresh per-call cap."""

    def test_cap_spans_the_retry(self) -> None:
        runtime, _ = self.runtime()
        _no_pauses(runtime)
        made = {"n": 0, "attempts": 0}

        async def dies_then_continues(ig):
            made["attempts"] += 1
            for _ in range(15 if made["attempts"] == 1 else 25):
                await ig.pacer.before_request()
                made["n"] += 1
            if made["attempts"] == 1:
                raise BrowserUnavailableError("target closed", retryable=True)

        with self.assertRaises(BudgetExceededError):
            _run(runtime.run("carol", dies_then_continues))
        self.assertEqual(made["attempts"], 2)
        self.assertEqual(made["n"], 20)


class OwnIdWithoutDsUserIdTests(unittest.TestCase):
    """5. ds_user_id can expire while the session works: the own id comes
    from the page's actorID, and auth_status only needs a live sessionid."""

    def test_viewer_id_from_actor_id_not_the_cookie(self) -> None:
        from test_inpage import _client

        from _fakes import FakeContext

        client, page, _ = _client([])
        # Session cookie set, ds_user_id gone (expired) — or stale.
        for cookies in ({"sessionid": "S", "csrftoken": "C"},
                        {"sessionid": "S", "csrftoken": "C", "ds_user_id": "111"}):
            with self.subTest(cookies=sorted(cookies)):
                client._actx.context = FakeContext(cookies)
                client._actx.tokens = None
                client._actx.last_html = None
                self.assertEqual(_run(client.viewer_id()), "17841400000000001")

    def test_cookie_is_the_fallback(self) -> None:
        from test_inpage import _client

        client, page, _ = _client([], html="<html>no tokens</html>")
        self.assertEqual(_run(client.viewer_id()), "999")  # FakeContext's ds_user_id

    def test_auth_status_with_expired_ds_user_id(self) -> None:
        import time

        from instagram_sdk.auth import session_summary

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "carol_instagram_state.json"
            past, future = time.time() - 86400 * 90, time.time() + 86400 * 180

            def write(session_expires):
                path.write_text(json.dumps({"cookies": [
                    {"name": "sessionid", "value": "S", "expires": session_expires},
                    {"name": "ds_user_id", "value": "9", "expires": past}]}))
                return session_summary(path)

            ok = write(future)
            self.assertTrue(ok["authenticated"])
            self.assertEqual(ok["missing_cookies"], [])
            self.assertEqual(ok["expired_cookies"], ["ds_user_id"])
            dead = write(past)
            self.assertFalse(dead["authenticated"])
            self.assertEqual(dead["missing_cookies"], ["sessionid"])

    def test_account_id_prefers_the_sidecar(self) -> None:
        from instagram_sdk.accounts import build_meta, write_meta

        with tempfile.TemporaryDirectory() as tmp:
            path = _session(Path(tmp))  # cookie ds_user_id = 9 (stale)
            write_meta(path, build_meta(username="carol", user_id="42", locale="en-US",
                                        timezone_id="Etc/UTC",
                                        viewport={"width": 1, "height": 1}))
            self.assertEqual(Account.from_session(path).user_id, "42")


if __name__ == "__main__":
    unittest.main()
