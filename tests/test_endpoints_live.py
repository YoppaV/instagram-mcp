"""Live integration test against the real Instagram web app (opt-in).

Touches the network and needs a valid session. Skipped unless
``RUN_LIVE_TESTS=1``::

    RUN_LIVE_TESTS=1 \\
        INSTAGRAM_LIVE_SESSION_FILE=/path/to/<handle>_instagram_state.json \\
        INSTAGRAM_LIVE_TARGET=<public_username_with_40plus_reels> \\
        venv/bin/python -m unittest tests.test_endpoints_live -v

Be conservative: wait at least 30 s between runs. A block error stops it.
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
    """A public account with >40 reels yields >= 40 distinct reels."""

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
                async with browser.page() as ig:
                    return await profile.fetch_reels(ig, username=target, limit=EXPECTED_MIN_REELS)
            finally:
                await browser.shutdown()

        reels = asyncio.run(run())
        self.assertGreaterEqual(len(reels), EXPECTED_MIN_REELS)
        shortcodes = [r.shortcode for r in reels]
        self.assertTrue(all(shortcodes))
        self.assertEqual(len(set(shortcodes)), len(shortcodes))


if __name__ == "__main__":
    unittest.main()
