"""Several accounts in one runtime, the cross-process lock, cursors and budget."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient, load  # noqa: E402
from test_runtime import FakePW  # noqa: E402

from instagram_sdk.accounts import AccountRegistry, build_meta, write_meta  # noqa: E402
from instagram_sdk.browser import BrowserManager  # noqa: E402
from instagram_sdk.cursors import (  # noqa: E402
    CursorState,
    PageResult,
    decode_cursor,
    encode_cursor,
    envelope,
    paginate,
)
from instagram_sdk.endpoints import saved  # noqa: E402
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    BudgetExceededError,
    OperationFailedError,
)
from instagram_sdk.locks import FileLock  # noqa: E402
from instagram_sdk.runtime import Runtime  # noqa: E402
from instagram_sdk.settings import Settings  # noqa: E402


def _session(directory: Path, handle: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{handle}_instagram_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "S"},
                                            {"name": "ds_user_id", "value": "9"}]}))
    return path


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sessions = self.dir / "sessions"
        _session(self.sessions, "carol")
        _session(self.sessions, "dave")
        self.settings = replace(
            Settings.from_env(), session_file=None, session_dir=self.sessions, username="",
            default_account="carol", state_dir=self.dir / "state", idle_timeout_s=0,
            max_requests_per_hour=150, max_requests_per_day=800,
        )

    def runtime(self, settings: Settings | None = None) -> tuple[Runtime, FakePW]:
        settings = settings or self.settings
        pw = FakePW()
        manager = BrowserManager(settings, playwright_factory=lambda: pw)
        runtime = Runtime(settings, registry=AccountRegistry(settings), browser=manager)
        return runtime, pw


class _NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _no_pauses(runtime: Runtime) -> list[str]:
    """Patch every new context's pacer so tests do not sleep; record switches."""
    switches: list[str] = []
    original = runtime.browser.context_for

    async def context_for(account):
        actx = await original(account)
        actx.pacer._sleep = _NoSleep()

        async def switch_pause() -> None:
            switches.append(account.handle)

        actx.pacer.switch_pause = switch_pause
        return actx

    runtime.browser.context_for = context_for
    return switches


class MultiAccountRuntimeTests(_Base):
    def test_one_context_per_account_one_browser_and_switch_pause(self) -> None:
        runtime, pw = self.runtime()
        switches = _no_pauses(runtime)
        seen = []

        async def who(ig):
            await ig.pacer.before_request()  # a request was made
            seen.append(ig.account)

        async def go():
            await runtime.run(None, who)
            await runtime.run("dave", who)
            await runtime.run("dave", who)
            await runtime.run("CAROL", who)

        asyncio.run(go())
        self.assertEqual(seen, ["carol", "dave", "dave", "carol"])
        self.assertEqual(len(pw.launched), 1)  # one Chromium
        self.assertEqual(len(pw.launched[0].contexts), 2)  # one context per account
        self.assertEqual(switches, ["dave", "carol"])  # pause only on a change

    def test_calls_on_different_accounts_never_overlap(self) -> None:
        runtime, _ = self.runtime()
        _no_pauses(runtime)
        active, peak = [0], [0]

        async def work(_ig):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.01)
            active[0] -= 1

        async def go():
            await asyncio.gather(*(runtime.run(a, work) for a in ("carol", "dave") * 3))

        asyncio.run(go())
        self.assertEqual(peak[0], 1)

    def test_sidecar_fingerprint_reaches_the_context(self) -> None:
        write_meta(self.sessions / "dave_instagram_state.json",
                   build_meta(username="dave", user_id="9", locale="es-ES",
                              timezone_id="Europe/Madrid", viewport={"width": 1366, "height": 768}))
        runtime, pw = self.runtime()
        _no_pauses(runtime)
        asyncio.run(runtime.run("dave", lambda ig: asyncio.sleep(0)))
        kwargs = pw.launched[0].last_kwargs
        self.assertEqual(kwargs["locale"], "es-ES")
        self.assertEqual(kwargs["timezone_id"], "Europe/Madrid")
        self.assertEqual(kwargs["viewport"], {"width": 1366, "height": 768})
        self.assertTrue(kwargs["storage_state"].endswith("dave_instagram_state.json"))

    def test_budget_is_persisted_and_enforced_across_runtimes(self) -> None:
        settings = replace(self.settings, max_requests_per_hour=3)
        runtime, _ = self.runtime(settings)
        _no_pauses(runtime)

        async def two(ig):
            await ig.pacer.before_request()
            await ig.pacer.before_request()

        asyncio.run(runtime.run("carol", two))
        # A new process (new runtime) sees the same counters.
        runtime2, _ = self.runtime(settings)
        _no_pauses(runtime2)
        with self.assertRaises(BudgetExceededError):
            asyncio.run(runtime2.run("carol", two))
        asyncio.run(runtime2.run("dave", two))  # each account has its own

    def test_failure_brake_through_the_runtime(self) -> None:
        runtime, _ = self.runtime()
        _no_pauses(runtime)

        async def fail(ig):
            await ig.pacer.before_request()
            raise OperationFailedError("profile_info", "HTTP 500")

        for _ in range(3):
            with self.assertRaises(OperationFailedError):
                asyncio.run(runtime.run("carol", fail))
        with self.assertRaises(AccountBlockedError) as ctx:
            asyncio.run(runtime.run("carol", fail))
        self.assertEqual(ctx.exception.kind, "failures")

    def test_file_lock_is_held_during_a_call(self) -> None:
        runtime, _ = self.runtime()
        _no_pauses(runtime)
        held = []

        async def probe(_ig):
            other = FileLock(self.settings.state_dir / "nav.lock")
            held.append(not other.try_acquire())
            other.release()

        asyncio.run(runtime.run("carol", probe))
        self.assertEqual(held, [True])
        free = FileLock(self.settings.state_dir / "nav.lock")
        self.assertTrue(free.try_acquire())  # released after the call
        free.release()

    def test_waits_for_another_process_then_times_out(self) -> None:
        import instagram_sdk.runtime as runtime_mod

        runtime, _ = self.runtime()
        other = FileLock(self.settings.state_dir / "nav.lock")
        self.assertTrue(other.try_acquire())
        self.addCleanup(other.release)
        old = runtime_mod.LOCK_WAIT_S
        runtime_mod.LOCK_WAIT_S = 0.3
        self.addCleanup(setattr, runtime_mod, "LOCK_WAIT_S", old)
        with self.assertRaises(OperationFailedError) as ctx:
            asyncio.run(runtime.run("carol", lambda ig: asyncio.sleep(0)))
        self.assertIn("another instagram-mcp process", str(ctx.exception))
        # The process lock was released too: the next call can proceed.
        other.release()
        _no_pauses(runtime)
        asyncio.run(runtime.run("carol", lambda ig: asyncio.sleep(0)))


class FileLockTests(unittest.TestCase):
    def test_exclusive_between_handles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "nav.lock"
            a, b = FileLock(path), FileLock(path)
            self.assertTrue(a.try_acquire())
            self.assertFalse(b.try_acquire())
            self.assertFalse(asyncio.run(b.acquire(0.25)))
            a.release()
            self.assertTrue(asyncio.run(b.acquire(1)))
            b.release()


class CursorTests(unittest.TestCase):
    params = {"username": "carol", "include_reels": False}

    def test_round_trip(self) -> None:
        state = CursorState("QVFE_abc==", 3)
        token = encode_cursor("get_user_posts", "Carol", self.params, state)
        self.assertNotIn("=", token)
        self.assertEqual(decode_cursor(token, "get_user_posts", "carol", self.params), state)
        self.assertIsNone(decode_cursor(None, "get_user_posts", "carol", self.params))
        self.assertIsNone(encode_cursor("get_user_posts", "carol", self.params, None))

    def test_other_query_account_or_tool_rejected(self) -> None:
        token = encode_cursor("get_user_posts", "carol", self.params, CursorState("U", 0))
        for tool, account, params in (
            ("get_user_reels", "carol", self.params),
            ("get_user_posts", "dave", self.params),
            ("get_user_posts", "carol", {**self.params, "include_reels": True}),
        ):
            with self.subTest(tool=tool, account=account), self.assertRaises(ValueError):
                decode_cursor(token, tool, account, params)
        for bad in ("%%%", "e30", "W10"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                decode_cursor(bad, "get_user_posts", "carol", self.params)

    def test_resume_mid_page_without_overlap(self) -> None:
        pages = {None: ([f"a{i}" for i in range(5)], "P2"), "P2": (["b0", "b1"], None)}

        async def fetch(upstream):
            return pages[upstream]

        async def go():
            first = await paginate(fetch, limit=3, max_pages=8, key_of=lambda x: x)
            token = encode_cursor("t", "carol", {}, first.next)
            second = await paginate(fetch, limit=3, max_pages=8, key_of=lambda x: x,
                                    start=decode_cursor(token, "t", "carol", {}))
            return first, second

        first, second = asyncio.run(go())
        self.assertEqual(first.items, ["a0", "a1", "a2"])
        self.assertEqual(second.items, ["a3", "a4", "b0"])
        self.assertEqual(second.next, CursorState("P2", 1))

    def test_envelope_shape(self) -> None:
        out = envelope(PageResult(["x"], CursorState("U", 0), "page_cap"), tool="t",
                       account="carol", params={}, serialize=list)
        self.assertEqual(set(out), {"items", "next_cursor", "stopped"})
        self.assertEqual(out["stopped"], "page_cap")
        self.assertIsInstance(out["next_cursor"], str)


class CollectionCursorTests(unittest.TestCase):
    def test_graphql_fallback_cursor_resumes_on_graphql(self) -> None:
        payload = load("gql_collection_media.json")
        media = payload["data"]["fetch__MediaCollection"]["media"]
        media["page_info"] = {"has_next_page": True, "end_cursor": "GQLCUR"}
        gone = OperationFailedError("collection_feed", "fail")
        ig = FakeClient(rest={"collection_feed": [gone]},
                        graphql={"saved_collection": [payload]})
        page = asyncio.run(saved.fetch_saved_page(ig, collection_id="17900000000000001",
                                                  limit=2))
        self.assertEqual(page.next, CursorState("gql:GQLCUR", 0))
        payload2 = load("gql_collection_media.json")
        ig2 = FakeClient(graphql={"saved_collection": [payload2]})
        asyncio.run(saved.fetch_saved_page(ig2, collection_id="17900000000000001",
                                           limit=2, start=page.next))
        # Straight to GraphQL with Instagram's own cursor; no REST attempt.
        self.assertEqual(ig2.calls[0][:2], ("graphql", "saved_collection"))
        self.assertEqual(ig2.calls[0][2]["after"], "GQLCUR")


if __name__ == "__main__":
    unittest.main()
