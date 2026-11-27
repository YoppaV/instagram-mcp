"""InPageClient over a fake page: request shape, classification, retries."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeContext, FakePage, fetch_result, load, text  # noqa: E402

from instagram_sdk.browser import AccountContext  # noqa: E402
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    BrowserUnavailableError,
    DocIdStaleError,
    EndpointGoneError,
    OperationFailedError,
    SessionExpiredError,
)
from instagram_sdk.inpage import InPageClient, parse_body  # noqa: E402
from instagram_sdk.pacer import Pacer  # noqa: E402


async def _no_sleep(_s: float) -> None:
    return None


def _client(results, *, html=None, nav_url=None):
    page = FakePage(results, html=html or text("html_tokens.html"), nav_url=nav_url)
    actx = AccountContext(
        handle="carol", context=FakeContext(), page=page, session_fp=None,
        pacer=Pacer(sleep=_no_sleep),
    )
    blocks = []

    async def on_block(signal):
        blocks.append(signal)
        return AccountBlockedError("carol", signal.kind, signal.detail)

    client = InPageClient(actx, on_block=on_block)
    client.new_call()
    return client, page, blocks


def _run(coro):
    return asyncio.run(coro)


PROFILE = load("gql_profile.json")


class GraphQLRequestTests(unittest.TestCase):
    def test_body_and_headers_are_the_minimal_verified_set(self) -> None:
        client, page, _ = _client([fetch_result(PROFILE)])
        user = _run(client.graphql("profile_info", id="500"))
        self.assertEqual(user["username"], "carol")
        self.assertEqual(page.gotos, ["https://www.instagram.com/"])  # bootstrap
        sent = page.fetches[0]
        self.assertEqual((sent["url"], sent["method"]), ("/api/graphql", "POST"))
        form = {k: v[0] for k, v in parse_qs(sent["body"]).items()}
        self.assertEqual(
            set(form),
            {"av", "__user", "__a", "__comet_req", "fb_dtsg", "jazoest", "lsd",
             "fb_api_caller_class", "fb_api_req_friendly_name", "variables",
             "server_timestamps", "doc_id"},
        )
        self.assertEqual(form["fb_dtsg"], "FAKE:DTSG_TOKEN")
        self.assertEqual(form["jazoest"], "2" + str(sum(map(ord, "FAKE:DTSG_TOKEN"))))
        self.assertEqual(form["av"], "17841400000000001")
        self.assertEqual(form["doc_id"], "29609640381972919")
        self.assertIn('"id":"500"', form["variables"])
        self.assertEqual(
            sent["headers"],
            {"Content-Type": "application/x-www-form-urlencoded",
             "X-IG-App-ID": "111111111111111", "X-CSRFToken": "CSRF_COOKIE",
             "X-FB-LSD": "FAKE_LSD", "X-FB-Friendly-Name": "PolarisProfilePageContentQuery"},
        )

    def test_timeline_goes_to_graphql_query_with_ig_did(self) -> None:
        client, page, _ = _client([fetch_result(load("gql_timeline.json"))])
        _run(client.graphql("home_page", after="C1", first=12))
        sent = page.fetches[0]
        self.assertEqual(sent["url"], "/graphql/query")
        variables = parse_qs(sent["body"])["variables"][0]
        self.assertIn('"device_id":"DID-1"', variables)
        self.assertNotIn("feed_view_info", variables)

    def test_optional_hole_is_dropped_on_first_page(self) -> None:
        client, page, _ = _client([fetch_result(load("gql_collections_v2.json"))])
        _run(client.graphql("saved_collections", after=None))
        self.assertNotIn('"after"', parse_qs(page.fetches[0]["body"])["variables"][0])


class ClassificationTests(unittest.TestCase):
    def test_root_null_vs_absent(self) -> None:
        client, _, _ = _client([fetch_result({"data": {"user": None}})])
        self.assertIsNone(_run(client.graphql("profile_info", id="1")))
        client, _, _ = _client([fetch_result({"data": {"other": 1}})])
        with self.assertRaises(DocIdStaleError):
            _run(client.graphql("profile_info", id="1"))

    def test_stale_with_fresh_tokens_fails_after_one_send(self) -> None:
        client, page, _ = _client([fetch_result(load("err_gql_stale.json"))])
        with self.assertRaises(DocIdStaleError) as ctx:
            _run(client.graphql("profile_info", id="1"))
        self.assertIn("ops.py", str(ctx.exception))
        self.assertEqual(len(page.fetches), 1)

    def test_token_error_refreshes_tokens_once(self) -> None:
        raw = text("err_fb_token.txt")
        client, page, _ = _client([fetch_result(None, raw=raw), fetch_result(PROFILE)])
        user = _run(client.graphql("profile_info", id="500"))
        self.assertEqual(user["pk"], "500")
        self.assertEqual(len(page.gotos), 2)  # bootstrap + one refresh
        client, page, _ = _client([fetch_result(None, raw=raw), fetch_result(None, raw=raw)])
        with self.assertRaises(OperationFailedError):
            _run(client.graphql("profile_info", id="500"))
        self.assertEqual(len(page.fetches), 2)

    def test_not_logged_in_fb_error_is_session_expired(self) -> None:
        raw = 'for (;;);{"error":1357001,"errorSummary":"Not logged in"}'
        client, _, blocks = _client([fetch_result(None, raw=raw)])

        async def on_block(signal):
            blocks.append(signal)
            return SessionExpiredError("expired")

        client._on_block = on_block
        with self.assertRaises(SessionExpiredError):
            _run(client.graphql("profile_info", id="1"))
        self.assertEqual(blocks[0].kind, "session_expired")

    def test_network_error_retried_once(self) -> None:
        client, page, _ = _client([{"ok": False, "error": "TypeError"}, fetch_result(PROFILE)])
        self.assertIsNotNone(_run(client.graphql("profile_info", id="500")))
        self.assertEqual(len(page.fetches), 2)

    def test_parse_body_tolerates_prefix_and_chunks(self) -> None:
        self.assertEqual(parse_body('for (;;);{"a":1}'), {"a": 1})
        self.assertEqual(parse_body('{"a":1}\n{"b":2}'), {"a": 1})
        self.assertIsNone(parse_body("<html>"))


class BlockTests(unittest.TestCase):
    def test_block_signal_means_zero_retries(self) -> None:
        for name in ("err_wait.json", "err_checkpoint.json", "err_feedback.json"):
            with self.subTest(name=name):
                client, page, blocks = _client([fetch_result(load(name), status=400)])
                with self.assertRaises(AccountBlockedError):
                    _run(client.graphql("profile_info", id="1"))
                self.assertEqual(len(page.fetches), 1)
                self.assertEqual(len(blocks), 1)

    def test_wait_phrase_with_require_login_is_rate_limit(self) -> None:
        client, _, blocks = _client([fetch_result(load("err_wait.json"), status=200)])
        with self.assertRaises(AccountBlockedError):
            _run(client.rest("saved_posts"))
        self.assertEqual(blocks[0].kind, "rate_limited")

    def test_navigation_to_challenge_blocks(self) -> None:
        client, page, blocks = _client([], nav_url=lambda u: "https://www.instagram.com/challenge/abc/")
        with self.assertRaises(AccountBlockedError):
            _run(client.navigate_html("/p/X/"))
        self.assertEqual(blocks[0].kind, "challenge")


class RestTests(unittest.TestCase):
    def test_rest_only_needs_app_id(self) -> None:
        client, page, _ = _client([fetch_result(load("api_feed_saved.json"))])
        body = _run(client.rest("saved_posts", query={"max_id": "M1"}))
        self.assertEqual(len(body["items"]), 2)
        sent = page.fetches[0]
        self.assertEqual(sent["url"], "/api/v1/feed/saved/posts/?max_id=M1")
        self.assertEqual(sent["headers"], {"X-IG-App-ID": "111111111111111"})

    def test_404_html_is_endpoint_gone_not_a_block(self) -> None:
        client, _, blocks = _client([fetch_result(None, status=404, raw='<html class="not-logged-in">')])
        with self.assertRaises(EndpointGoneError):
            _run(client.rest("user_info", path_params={"user_id": "1"}))
        self.assertEqual(blocks, [])

    def test_target_closed_is_retryable_browser_error(self) -> None:
        client, _, _ = _client([RuntimeError("Target page, context or browser has been closed")])
        with self.assertRaises(BrowserUnavailableError) as ctx:
            _run(client.rest("saved_posts"))
        self.assertTrue(ctx.exception.retryable)


class CapTests(unittest.TestCase):
    def test_per_call_cap(self) -> None:
        from instagram_sdk.errors import BudgetExceededError

        client, page, _ = _client([fetch_result(PROFILE)] * 5)
        client.pacer.max_requests_per_call = 2  # bootstrap + 1 fetch
        _run(client.graphql("profile_info", id="500"))
        with self.assertRaises(BudgetExceededError):
            _run(client.graphql("profile_info", id="500"))


if __name__ == "__main__":
    unittest.main()
