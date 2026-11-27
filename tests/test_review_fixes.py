"""Regression tests for the PR #5 review (one per finding, no network)."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient, FakeContext, fetch_result, load, text  # noqa: E402
from test_inpage import _client  # noqa: E402
from test_runtime import FakePW, _Base  # noqa: E402

from instagram_sdk.accounts import AccountStateStore  # noqa: E402
from instagram_sdk.browser import BrowserManager  # noqa: E402
from instagram_sdk.endpoints import saved  # noqa: E402
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    DocIdStaleError,
    OperationFailedError,
    SessionExpiredError,
)
from instagram_sdk.guard import BlockSignal  # noqa: E402
from instagram_sdk.runtime import Runtime  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class QueuedCallsAfterBlockTests(_Base):
    """1. Calls waiting on the lock must re-check the state once inside."""

    def test_queued_call_does_not_run_after_a_block(self) -> None:
        manager = BrowserManager(self.settings, playwright_factory=lambda: FakePW())
        runtime = Runtime(self.settings, session_source=lambda: self.session, browser=manager)
        ran = []

        async def first(ig):
            await asyncio.sleep(0.01)
            raise await ig._on_block(BlockSignal("rate_limited", "HTTP 429"))

        async def queued(ig):
            ran.append(1)

        async def go():
            return await asyncio.gather(runtime.run(None, first), runtime.run(None, queued),
                                        return_exceptions=True)

        results = _run(go())
        self.assertTrue(all(isinstance(r, AccountBlockedError) for r in results), results)
        self.assertEqual(ran, [])
        history = json.loads((self.dir / "state/accounts/carol.json").read_text())["history"]
        self.assertEqual(len(history), 1)  # one strike, not one per queued call


class NullRootWithErrorsTests(unittest.TestCase):
    """2. Null root + errors is a failure, not an empty result."""

    def test_null_root_with_errors_is_stale(self) -> None:
        body = {"data": {"user": None}, "errors": [{"message": "field failed"}]}
        client, _, _ = _client([fetch_result(body)])
        with self.assertRaises(DocIdStaleError):
            _run(client.graphql("profile_info", id="1"))


class NavigationErrorStatusTests(unittest.TestCase):
    """3. Navigation >= 400 (not 404) is an error; JSON wait body is a block."""

    def test_403_with_wait_json_blocks(self) -> None:
        client, page, blocks = _client([])
        page.nav_status = 403
        page.nav_body = json.dumps(load("err_wait.json"))
        with self.assertRaises(AccountBlockedError):
            _run(client.navigate_html("/p/X/"))
        self.assertEqual(blocks[0].kind, "rate_limited")

    def test_5xx_is_an_error_not_none(self) -> None:
        client, page, blocks = _client([])
        page.nav_status = 502
        page.nav_body = "<html>Bad gateway</html>"
        with self.assertRaises(OperationFailedError):
            _run(client.navigate_html("/p/X/"))
        self.assertEqual(blocks, [])

    def test_404_still_returns_html(self) -> None:
        client, page, _ = _client([])
        page.nav_status = 404
        self.assertIn("DTSG", _run(client.navigate_html("/p/X/")))


class UnwritableStateTests(_Base):
    """4. A block applies (in memory) and closes the context even if disk fails.

    Since PR #6's review this needs the explicit opt-in
    ``INSTAGRAM_ALLOW_DEGRADED_STATE``; without it the call is refused before
    any request (``test_review6``).
    """

    def test_block_applies_when_state_dir_is_not_writable(self) -> None:
        import os

        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions and a non-root user")
        bad = self.dir / "readonly_state"
        bad.mkdir()
        os.chmod(bad, 0o555)
        self.addCleanup(os.chmod, bad, 0o755)
        from dataclasses import replace

        settings = replace(self.settings, state_dir=bad, allow_degraded_state=True)
        pw = FakePW()
        manager = BrowserManager(settings, playwright_factory=lambda: pw)
        runtime = Runtime(settings, session_source=lambda: self.session, browser=manager)

        async def blocked(ig):
            raise await ig._on_block(BlockSignal("checkpoint", "checkpoint_required"))

        with self.assertRaises(AccountBlockedError):
            _run(runtime.run(None, blocked))
        self.assertTrue(pw.launched[0].contexts[0].closed)
        with self.assertRaises(AccountBlockedError):
            _run(runtime.run(None, lambda ig: asyncio.sleep(0)))


class CollectionIdValidationTests(unittest.TestCase):
    """5. collection_id cannot smuggle a path."""

    def test_rejects_non_numeric_ids(self) -> None:
        for bad in ("../../challenge/x", "1/../../x", "abc", "1?x=1"):
            with self.subTest(bad=bad):
                ig = FakeClient()
                with self.assertRaises(ValueError):
                    _run(saved.fetch_saved(ig, collection_id=bad, limit=1))
                self.assertEqual(ig.calls, [])

    def test_rest_rejects_unsafe_path_params(self) -> None:
        client, page, _ = _client([])
        with self.assertRaises(ValueError):
            _run(client.rest("collection_feed", path_params={"collection_id": "../x"}))
        self.assertEqual(page.fetches, [])


class FacebookEnvelopeBlockTests(unittest.TestCase):
    """(a) FB error envelope saying 'temporarily blocked' is a block, not TOKEN."""

    def test_temporarily_blocked_envelope(self) -> None:
        raw = ('for (;;);{"error":1390008,"errorSummary":"You\\u2019re Temporarily Blocked",'
               '"errorDescription":"It looks like you were misusing this feature."}')
        client, page, blocks = _client([fetch_result(None, raw=raw)])
        with self.assertRaises(AccountBlockedError):
            _run(client.graphql("profile_info", id="1"))
        self.assertEqual(len(page.fetches), 1)
        self.assertEqual(blocks[0].kind, "rate_limited")


class ChromeErrorTabTests(unittest.TestCase):
    """(b) A tab left on chrome-error:// is not a loaded document."""

    def test_bootstrap_renavigates(self) -> None:
        client, page, _ = _client([fetch_result(load("api_feed_saved.json"))])
        _run(client.bootstrap())
        page.url = "chrome-error://chromewebdata/"
        client._actx.tokens = None
        _run(client.rest("saved_posts"))
        self.assertEqual(len(page.gotos), 2)


class LoggedOutHomeTests(unittest.TestCase):
    """(c) Login page served with 200 and no redirect → session expired."""

    def test_actor_zero_is_session_expired(self) -> None:
        html = text("html_tokens.html").replace("17841400000000001", "0")
        client, _, blocks = _client([], html=html)

        async def on_block(signal):
            blocks.append(signal)
            return SessionExpiredError("expired")

        client._on_block = on_block
        with self.assertRaises(SessionExpiredError):
            _run(client.graphql("profile_info", id="1"))
        self.assertEqual(blocks[0].kind, "session_expired")

    def test_missing_sessionid_cookie_is_session_expired(self) -> None:
        client, _, blocks = _client([])
        client._actx.context = FakeContext({"csrftoken": "c"})

        async def on_block(signal):
            blocks.append(signal)
            return SessionExpiredError("expired")

        client._on_block = on_block
        with self.assertRaises(SessionExpiredError):
            _run(client.navigate_html("/"))


if __name__ == "__main__":
    unittest.main()
