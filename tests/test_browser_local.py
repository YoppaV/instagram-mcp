"""Local Chromium checks with NO traffic to Instagram (opt-in, not in CI).

Every request to ``https://www.instagram.com/**`` is answered by a
``context.route`` that fulfills canned HTML/JSON, so nothing leaves the
machine. Checks what the doubles cannot: ``FETCH_JS`` end to end, the
resource blocking, the Client Hints override and crash recovery.

Run with::

    RUN_BROWSER_TESTS=1 PYTHONPATH=src venv/bin/python -m unittest tests.test_browser_local -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import auth  # noqa: E402
from instagram_sdk.accounts import Account  # noqa: E402
from instagram_sdk.browser import BrowserManager  # noqa: E402
from instagram_sdk.inpage import InPageClient  # noqa: E402
from instagram_sdk.settings import Settings  # noqa: E402

HTML = (
    "<!doctype html><html><head><link rel=stylesheet href='/s.css'>"
    "<script src='/a.js'></script></head><body><img src='/i.png'>"
    '<script type="application/json">{"x":["DTSGInitialData",[],{"token":"FAKE_DTSG"}],'
    '"y":["LSD",[],{"token":"FAKE_LSD"}],"actorID":"17841400000000000"}</script>'
    "</body></html>"
)


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "set RUN_BROWSER_TESTS=1")
class LocalBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.session = self.dir / "local_instagram_state.json"
        self.session.write_text(json.dumps({"cookies": [], "origins": []}))
        self.settings = replace(
            Settings.from_env(), state_dir=self.dir / "state", idle_timeout_s=0,
            headless=True, locale="es-ES",
        )

    async def _with_manager(self, fn):
        manager = BrowserManager(self.settings)
        try:
            return await fn(manager)
        finally:
            await manager.shutdown()

    def test_fetch_blocking_and_client_hints(self) -> None:
        seen: list[tuple[str, str, dict]] = []

        async def scenario(manager: BrowserManager):
            actx = await manager.context_for(Account.from_session(self.session))

            async def fulfill(route):
                req = route.request
                seen.append((req.resource_type, req.url, await req.all_headers()))
                if req.url.endswith("/api/v1/test/"):
                    await route.fulfill(status=200, content_type="application/json",
                                        body='for (;;);{"status":"ok","n":1}')
                else:
                    await route.fulfill(status=200, content_type="text/html", body=HTML)

            await actx.context.route("https://www.instagram.com/**", fulfill)

            async def on_block(signal):
                return RuntimeError(signal.kind)

            client = InPageClient(actx, on_block=on_block)
            client.pacer._sleep = lambda _s: asyncio.sleep(0)
            client._table = {**client._table}
            from instagram_sdk.ops import RestOp
            client._table["t"] = RestOp(name="t", path="/api/v1/test/")
            body = await client.rest("t")
            brands = await actx.page.evaluate("() => navigator.userAgentData.brands")
            ua = await actx.page.evaluate("() => navigator.userAgent")
            return body, brands, ua, actx.tokens, manager._browser_version

        body, brands, ua, tokens, version = asyncio.run(self._with_manager(scenario))
        self.assertEqual(body, {"status": "ok", "n": 1})
        types = [t for t, _u, _h in seen]
        self.assertEqual(types, ["document", "fetch"], seen)
        self.assertEqual(tokens.fb_dtsg, "FAKE_DTSG")
        doc_headers = seen[0][2]
        self.assertNotIn("Headless", doc_headers.get("sec-ch-ua", ""))
        self.assertIn('"Google Chrome"', doc_headers.get("sec-ch-ua", ""))
        self.assertNotIn("Headless", ua)
        major = int(version.split(".")[0])
        self.assertEqual(brands, auth.greased_brands(major))
        print("\nsec-ch-ua:", doc_headers.get("sec-ch-ua"),
              "| platform:", doc_headers.get("sec-ch-ua-platform"),
              "| accept-language:", doc_headers.get("accept-language"))

    def test_native_brand_order_matches_algorithm(self) -> None:
        async def scenario():
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context()
                page = await ctx.new_page()
                await ctx.route("https://www.instagram.com/**",
                                lambda r: r.fulfill(status=200, body="<html></html>",
                                                    content_type="text/html"))
                await page.goto("https://www.instagram.com/")
                native = await page.evaluate("() => navigator.userAgentData.brands")
                version = browser.version
                await browser.close()
                return native, version

        native, version = asyncio.run(scenario())
        major = int(version.split(".")[0])
        expected = auth.greased_brands(major, brand="HeadlessChrome")
        self.assertEqual(native, expected)

    def test_recovers_after_browser_dies(self) -> None:
        async def scenario(manager: BrowserManager):
            acct = Account.from_session(self.session)
            first = await manager.context_for(acct)
            await manager._browser.close()  # simulate Chromium dying
            await asyncio.sleep(0.2)
            second = await manager.context_for(acct)
            return first, second, second.alive()

        first, second, alive = asyncio.run(self._with_manager(scenario))
        self.assertIsNot(first, second)
        self.assertTrue(alive)


if __name__ == "__main__":
    unittest.main()
