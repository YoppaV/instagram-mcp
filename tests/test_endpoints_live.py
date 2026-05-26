"""Live integration tests against the real Instagram web app.

These tests touch the network and require a valid session storage_state.
They are skipped unless ``RUN_LIVE_TESTS=1`` is set in the environment.

Run with::

    RUN_LIVE_TESTS=1 \\
        INSTAGRAM_LIVE_SESSION_FILE=/path/to/<handle>_instagram_state.json \\
        INSTAGRAM_LIVE_TARGET=<public_username_with_40plus_reels> \\
        venv/bin/python -m unittest tests.test_endpoints_live -v

Be conservative: wait at least 30 s between consecutive runs to avoid
tripping Instagram's anti-bot heuristics.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path

from instagram_sdk.browser import BrowserSession
from instagram_sdk.endpoints import profile

EXPECTED_MIN_REELS = 40


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_TESTS") == "1",
    "live test against Instagram — set RUN_LIVE_TESTS=1 to enable",
)
class LiveReelsPaginationTest(unittest.TestCase):
    """Regression test for the Reels-tab pagination fix.

    A public account with >40 reels must yield at least 40 distinct reels with
    non-empty shortcodes. Pre-fix this returned ~12 — only the initial
    server-render's batch.
    """

    def test_fetch_reels_paginates_beyond_initial_page(self) -> None:
        session_env = os.environ.get("INSTAGRAM_LIVE_SESSION_FILE", "").strip()
        target = os.environ.get("INSTAGRAM_LIVE_TARGET", "").strip()
        if not session_env:
            self.skipTest("INSTAGRAM_LIVE_SESSION_FILE not set")
        if not target:
            self.skipTest("INSTAGRAM_LIVE_TARGET not set")
        session_path = Path(session_env).expanduser()
        if not session_path.exists():
            self.skipTest(f"session file missing: {session_path}")

        async def run() -> list:
            browser = BrowserSession(session_path, idle_timeout_s=0, headless=True)
            try:
                async with browser.page() as page:
                    return await profile.fetch_reels(
                        page,
                        username=target,
                        limit=EXPECTED_MIN_REELS,
                    )
            finally:
                await browser.shutdown()

        reels = asyncio.run(run())
        self.assertGreaterEqual(
            len(reels),
            EXPECTED_MIN_REELS,
            f"expected >= {EXPECTED_MIN_REELS} reels, got {len(reels)}",
        )
        shortcodes = [r.shortcode for r in reels]
        self.assertTrue(
            all(shortcodes),
            f"empty shortcode in result: {shortcodes}",
        )
        self.assertEqual(
            len(set(shortcodes)),
            len(shortcodes),
            f"duplicate shortcodes: {shortcodes}",
        )


if __name__ == "__main__":
    unittest.main()
