"""Login validation, browser fingerprint, per-call session and limit cap."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import auth, browser as browser_mod  # noqa: E402
from instagram_sdk import server as server_mod  # noqa: E402
from instagram_sdk.auth import SessionExpiredError  # noqa: E402


class BrowserEnvTests(unittest.TestCase):
    def test_locale_and_timezone_are_read_at_call_time(self) -> None:
        env = {"BROWSER_LOCALE": "es-ES", "BROWSER_TIMEZONE_ID": "Europe/Madrid"}
        with mock.patch.dict(os.environ, env):
            options = auth.browser_context_options("147.0.7727.15")
        self.assertEqual(options["locale"], "es-ES")
        self.assertEqual(options["timezone_id"], "Europe/Madrid")

    def test_blank_values_fall_back_to_defaults(self) -> None:
        env = {"BROWSER_LOCALE": "  ", "BROWSER_TIMEZONE_ID": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(auth.browser_locale(), "en-US")
            self.assertEqual(auth.browser_timezone_id(), "Etc/UTC")


class UserAgentTests(unittest.TestCase):
    def test_matches_real_browser_and_os(self) -> None:
        cases = {
            "Windows": "Windows NT 10.0",
            "Darwin": "Macintosh",
            "Linux": "X11; Linux",
        }
        for system, token in cases.items():
            with self.subTest(system=system):
                agent = auth.build_user_agent("147.0.7727.15", system)
                self.assertIn(token, agent)
                self.assertIn("Chrome/147.0.0.0", agent)
                self.assertNotIn("Headless", agent)


class ValidateHandleTests(unittest.TestCase):
    def test_accepts_and_strips_at(self) -> None:
        self.assertEqual(auth.validate_handle(" @juan.r_92 "), "juan.r_92")

    def test_rejects_email_and_malformed(self) -> None:
        for bad in ("me@example.com", "", "has space", "x" * 31):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                auth.validate_handle(bad)

    def test_login_rejects_email_before_opening_browser(self) -> None:
        from scripts import auth_login

        argv = ["auth_login", "--username", "me@example.com"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            auth_login, "_launch_and_save"
        ) as launch:
            self.assertEqual(auth_login.main(), 2)
        launch.assert_not_called()


class ResolveSessionHandleTests(unittest.TestCase):
    def test_real_username_wins_over_a_typo(self) -> None:
        self.assertEqual(auth.resolve_session_handle("juan", "juanr"), "juanr")

    def test_same_account_keeps_requested_spelling(self) -> None:
        self.assertEqual(auth.resolve_session_handle("JuanR", "juanr"), "JuanR")

    def test_unknown_account_keeps_requested(self) -> None:
        self.assertEqual(auth.resolve_session_handle("juan", None), "juan")

    def test_lookup_failure_is_not_fatal(self) -> None:
        page = mock.Mock()
        page.evaluate.side_effect = RuntimeError("navigating")
        self.assertIsNone(auth.fetch_logged_in_username(page, "1"))


class PerCallSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _write_session(self) -> Path:
        path = self.dir / "carol_instagram_state.json"
        cookies = [{"name": "sessionid"}, {"name": "ds_user_id"}]
        path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")
        return path

    def test_login_after_startup_is_seen_without_restart(self) -> None:
        env = {
            "INSTAGRAM_SESSION_DIR": str(self.dir),
            "INSTAGRAM_SESSION_FILE": "",
            "INSTAGRAM_USERNAME": "",
        }
        with mock.patch.dict(os.environ, env):
            before = asyncio.run(server_mod.auth_status())
            self._write_session()
            after = asyncio.run(server_mod.auth_status())
            handle = server_mod._resolve_own_handle()
        self.assertFalse(before["authenticated"])
        self.assertTrue(after["authenticated"])
        self.assertEqual(handle, "carol")

    def test_warm_context_is_reused_while_file_is_unchanged(self) -> None:
        path = self._write_session()
        session = browser_mod.BrowserSession(lambda: path)
        session._context = sentinel = object()
        session._loaded_fingerprint = browser_mod._fingerprint(path)
        self.assertIs(asyncio.run(session._ensure_started()), sentinel)

    def test_context_is_dropped_when_session_file_goes_away(self) -> None:
        path = self._write_session()
        session = browser_mod.BrowserSession(lambda: path)
        session._context = mock.AsyncMock()
        session._loaded_fingerprint = browser_mod._fingerprint(path)
        path.unlink()
        with self.assertRaises(SessionExpiredError):
            asyncio.run(session._ensure_started())
        self.assertIsNone(session._context)


class ClampLimitTests(unittest.TestCase):
    def test_clamps_both_ends(self) -> None:
        self.assertEqual(server_mod._clamp_limit(10_000), server_mod.MAX_LIMIT)
        self.assertEqual(server_mod._clamp_limit(12), 12)
        self.assertEqual(server_mod._clamp_limit(0), 1)

    def test_tool_passes_clamped_limit_and_documents_it(self) -> None:
        with mock.patch.object(server_mod, "_browser") as fake, mock.patch.object(
            server_mod.home, "fetch", new=mock.AsyncMock(return_value=[])
        ) as fetch:
            fake.page.return_value.__aenter__ = mock.AsyncMock()
            fake.page.return_value.__aexit__ = mock.AsyncMock(return_value=False)
            asyncio.run(server_mod.get_home_feed(limit=500))
        self.assertEqual(fetch.call_args.kwargs["limit"], server_mod.MAX_LIMIT)
        self.assertIn("capped at 50", server_mod.get_home_feed.__doc__)


if __name__ == "__main__":
    unittest.main()
