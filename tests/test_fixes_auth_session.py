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

from instagram_sdk import auth  # noqa: E402
from instagram_sdk import server as server_mod  # noqa: E402


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


class ClientHintsTests(unittest.TestCase):
    def test_brands_match_chromium_vectors_without_headless(self) -> None:
        def header(major: int) -> str:
            return ", ".join(
                f'"{b["brand"]}";v="{b["version"]}"' for b in auth.greased_brands(major)
            )

        self.assertEqual(header(120), '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"')
        self.assertEqual(header(123), '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"')
        self.assertEqual(header(131), '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"')
        self.assertEqual(header(147), '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"')

    def test_metadata_is_coherent_with_ua_and_os(self) -> None:
        meta = auth.client_hints_metadata(
            "147.0.7727.15", "Linux", release="6.18.33.2-microsoft-standard-WSL2", machine="x86_64"
        )
        self.assertEqual(meta["platform"], "Linux")
        self.assertEqual(meta["platformVersion"], "6.18.33")
        self.assertEqual(meta["fullVersion"], "147.0.7727.15")
        self.assertNotIn("Headless", json.dumps(meta))
        full = {b["brand"]: b["version"] for b in meta["fullVersionList"]}
        self.assertEqual(full["Google Chrome"], "147.0.7727.15")
        self.assertEqual(full["Not.A/Brand"], "8.0.0.0")
        mac = auth.client_hints_metadata("147.0.1.2", "Darwin", machine="arm64", mac_version="14.5")
        self.assertEqual((mac["platform"], mac["platformVersion"], mac["architecture"]),
                         ("macOS", "14.5.0", "arm"))
        override = auth.user_agent_override("147.0.7727.15", "es-ES", "Windows")
        self.assertIn("Windows NT 10.0", override["userAgent"])
        self.assertEqual(override["platform"], "Win32")
        self.assertEqual(override["acceptLanguage"], "es-ES")


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
            "INSTAGRAM_STATE_DIR": str(self.dir / "state"),
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(server_mod, "_runtime", None):
            before = asyncio.run(server_mod.auth_status())
            self._write_session()
            after = asyncio.run(server_mod.auth_status())
        self.assertFalse(before["authenticated"])
        self.assertTrue(after["authenticated"])
        self.assertEqual(after["handle"], "carol")


class ClampLimitTests(unittest.TestCase):
    def test_clamps_both_ends(self) -> None:
        self.assertEqual(server_mod._clamp_limit(10_000), server_mod.MAX_LIMIT)
        self.assertEqual(server_mod._clamp_limit(12), 12)
        self.assertEqual(server_mod._clamp_limit(0), 1)

    def test_tool_passes_clamped_limit_and_documents_it(self) -> None:
        from instagram_sdk.cursors import PageResult

        class FakeRuntime:
            async def run(self, account, fn):
                return await fn(object())

        with mock.patch.object(server_mod, "get_runtime", return_value=FakeRuntime()), \
                mock.patch.object(server_mod.home, "fetch_home_page",
                                  new=mock.AsyncMock(return_value=PageResult())) as fetch:
            asyncio.run(server_mod.get_home_feed(limit=500))
        self.assertEqual(fetch.call_args.kwargs["limit"], server_mod.MAX_LIMIT)
        self.assertIn("capped at 50", server_mod.get_home_feed.__doc__)


if __name__ == "__main__":
    unittest.main()
