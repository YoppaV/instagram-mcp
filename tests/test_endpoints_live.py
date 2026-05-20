"""Live integration tests against the real Instagram web app.

These tests touch the network and require a valid session storage_state.
They are skipped unless ``RUN_LIVE_TESTS=1`` is set in the environment.

Run with::

    RUN_LIVE_TESTS=1 venv/bin/python -m unittest tests.test_endpoints_live -v

Be conservative: wait at least 30 s between consecutive runs to avoid
tripping Instagram's anti-bot heuristics. The session path is hard-coded to
the main checkout's session file because the worktree does not maintain its
own ``sessions/`` directory.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path

from instagram_sdk.browser import BrowserSession
from instagram_sdk.endpoints import profile

SESSION_PATH = Path(
    "/home/juanr/projects/mcp/instagram-mcp/sessions/jrdiaz03_instagram_state.json"
)
TARGET_USERNAME = "keshavsuki"
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
        if not SESSION_PATH.exists():
            self.skipTest(f"session file missing: {SESSION_PATH}")

        async def run() -> list:
            browser = BrowserSession(SESSION_PATH, idle_timeout_s=0, headless=True)
            try:
                async with browser.page() as page:
                    return await profile.fetch_reels(
                        page,
                        username=TARGET_USERNAME,
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
