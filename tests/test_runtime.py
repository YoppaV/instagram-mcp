"""Browser lifecycle, runtime and persistent block state, with a fake Playwright."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _fakes  # noqa: E402,F401

from instagram_sdk.accounts import Account, AccountStateStore  # noqa: E402
from instagram_sdk.browser import BrowserManager  # noqa: E402
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    BrowserUnavailableError,
    SessionExpiredError,
)
from instagram_sdk.guard import BlockSignal  # noqa: E402
from instagram_sdk.runtime import Runtime  # noqa: E402
from instagram_sdk.settings import Settings  # noqa: E402


class _Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event) -> None:
        for handler in self.handlers.get(event, []):
            handler(self)


class FakePWPage(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    async def route(self, *_a) -> None:
        pass


class FakeCDPSession:
    async def send(self, *_a) -> None:
        pass


class FakePWContext(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.page = FakePWPage()

    async def add_init_script(self, _s) -> None:
        pass

    async def new_page(self) -> FakePWPage:
        return self.page

    async def new_cdp_session(self, _p) -> FakeCDPSession:
        return FakeCDPSession()

    async def close(self) -> None:
        self.closed = True


class FakeBrowser(_Emitter):
    version = "147.0.7727.15"

    def __init__(self) -> None:
        super().__init__()
        self.connected = True
        self.contexts: list[FakePWContext] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **kwargs) -> FakePWContext:
        self.last_kwargs = kwargs
        ctx = FakePWContext()
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.connected = False


class FakePW:
    def __init__(self, fail_launch: bool = False) -> None:
        self.fail_launch = fail_launch
        self.stopped = 0
        self.launched: list[FakeBrowser] = []
        outer = self

        class Chromium:
            async def launch(self, **_kw):
                if outer.fail_launch:
                    raise RuntimeError("Executable doesn't exist")
                browser = FakeBrowser()
                outer.launched.append(browser)
                return browser

        self.chromium = Chromium()

    async def start(self) -> "FakePW":
        return self

    async def stop(self) -> None:
        self.stopped += 1


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.session = self.dir / "carol_instagram_state.json"
        self.session.write_text(json.dumps({"cookies": [{"name": "sessionid"}, {"name": "ds_user_id"}]}))
        self.settings = replace(Settings.from_env(), state_dir=self.dir / "state", idle_timeout_s=0)
        self.account = Account.from_session(self.session)


class BrowserLifecycleTests(_Base):
    def test_failed_launch_stops_the_driver(self) -> None:
        pw = FakePW(fail_launch=True)
        manager = BrowserManager(self.settings, playwright_factory=lambda: pw)
        with self.assertRaises(BrowserUnavailableError) as ctx:
            asyncio.run(manager.context_for(self.account))
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("playwright install chromium", str(ctx.exception))
        self.assertEqual(pw.stopped, 1)

    def test_disconnected_browser_is_relaunched(self) -> None:
        pw = FakePW()
        manager = BrowserManager(self.settings, playwright_factory=lambda: pw)

        async def go():
            first = await manager.context_for(self.account)
            browser = pw.launched[0]
            browser.connected = False
            browser.emit("disconnected")
            second = await manager.context_for(self.account)
            return first, second

        first, second = asyncio.run(go())
        self.assertIsNot(first, second)
        self.assertEqual(len(pw.launched), 2)
        self.assertTrue(first.dead)

    def test_context_reused_then_rebuilt_when_session_file_changes(self) -> None:
        pw = FakePW()
        manager = BrowserManager(self.settings, playwright_factory=lambda: pw)

        async def go():
            a = await manager.context_for(self.account)
            b = await manager.context_for(self.account)
            st = self.session.stat()
            os.utime(self.session, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            c = await manager.context_for(self.account)
            return a, b, c

        a, b, c = asyncio.run(go())
        self.assertIs(a, b)
        self.assertIsNot(a, c)
        self.assertTrue(pw.launched[0].contexts[0].closed)
        kwargs = pw.launched[0].last_kwargs
        self.assertEqual(kwargs["service_workers"], "block")
        self.assertNotIn("Headless", kwargs["user_agent"])

    def test_crashed_page_drops_context(self) -> None:
        pw = FakePW()
        manager = BrowserManager(self.settings, playwright_factory=lambda: pw)

        async def go():
            a = await manager.context_for(self.account)
            a.page._closed = True
            a.page.emit("crash")
            return a, await manager.context_for(self.account)

        a, b = asyncio.run(go())
        self.assertIsNot(a, b)


class RuntimeTests(_Base):
    def _runtime(self, pw: FakePW) -> Runtime:
        manager = BrowserManager(self.settings, playwright_factory=lambda: pw)
        return Runtime(self.settings, session_source=lambda: self.session, browser=manager)

    def test_calls_never_overlap(self) -> None:
        runtime = self._runtime(FakePW())
        active, peak = [0], [0]

        async def work(_ig):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.01)
            active[0] -= 1
            return 1

        async def go():
            return await asyncio.gather(*(runtime.run(None, work) for _ in range(3)))

        self.assertEqual(asyncio.run(go()), [1, 1, 1])
        self.assertEqual(peak[0], 1)

    def test_retryable_browser_error_retried_once(self) -> None:
        runtime = self._runtime(FakePW())
        calls = []

        async def flaky(_ig):
            calls.append(1)
            if len(calls) == 1:
                raise BrowserUnavailableError("gone", retryable=True)
            return "ok"

        self.assertEqual(asyncio.run(runtime.run(None, flaky)), "ok")
        self.assertEqual(len(calls), 2)

    def test_block_is_persisted_and_fails_fast(self) -> None:
        runtime = self._runtime(FakePW())

        async def blocked(ig):
            await ig._on_block(BlockSignal("checkpoint", "checkpoint_required"))

        async def never(_ig):
            raise AssertionError("must not run")

        asyncio.run(runtime.run(None, blocked))
        with self.assertRaises(AccountBlockedError) as ctx:
            asyncio.run(runtime.run(None, never))
        self.assertIsNone(ctx.exception.until)
        self.assertEqual(runtime.state.summary(self.account)["state"], "blocked")

    def test_missing_session_file(self) -> None:
        self.session.unlink()
        with self.assertRaises(SessionExpiredError):
            asyncio.run(self._runtime(FakePW()).run(None, lambda ig: None))


class AccountStateTests(_Base):
    def test_cooldown_12h_then_doubles_and_expires(self) -> None:
        now = [1_000_000.0]
        store = AccountStateStore(self.dir / "state", clock=lambda: now[0])
        err = store.record(self.account, BlockSignal("rate_limited", "429"))
        self.assertEqual(err.until, now[0] + 12 * 3600)
        with self.assertRaises(AccountBlockedError):
            store.check(self.account)
        now[0] += 12 * 3600 + 1
        store.check(self.account)  # expired → ok
        err = store.record(self.account, BlockSignal("feedback_required", "spam"))
        self.assertEqual(err.until - now[0], 24 * 3600)
        st = json.loads((self.dir / "state" / "accounts" / "carol.json").read_text())
        self.assertEqual(st["status"], "cooldown")

    def test_blocked_clears_on_relogin_or_manual_clear(self) -> None:
        store = AccountStateStore(self.dir / "state")
        store.record(self.account, BlockSignal("challenge", "/challenge/"))
        with self.assertRaises(AccountBlockedError):
            store.check(self.account)
        self.assertTrue(store.clear("carol"))
        store.check(self.account)
        store.record(self.account, BlockSignal("session_expired", "/accounts/login"))
        with self.assertRaises(SessionExpiredError):
            store.check(self.account)
        st = self.session.stat()
        os.utime(self.session, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
        store.check(self.account)  # new session file → cleared


if __name__ == "__main__":
    unittest.main()
