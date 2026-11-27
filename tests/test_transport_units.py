"""Pure pieces of the transport: guard, tokens, ops table, cursors, pacer."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import load, text  # noqa: E402

from instagram_sdk import guard, ops  # noqa: E402
from instagram_sdk.cursors import CursorState, paginate  # noqa: E402
from instagram_sdk.errors import BudgetExceededError, TokenError  # noqa: E402
from instagram_sdk.pacer import Pacer  # noqa: E402
from instagram_sdk.tokens import extract_tokens  # noqa: E402


def _fetch(body, status=200, url="https://www.instagram.com/api/v1/x/", text_head=""):
    return guard.check_fetch(status, url, False, "application/json", body, text_head)


class GuardTests(unittest.TestCase):
    def test_rules(self) -> None:
        cases = [
            (_fetch(None, status=429), "rate_limited"),
            (_fetch(load("err_wait.json")), "rate_limited"),  # beats require_login
            (_fetch(load("err_feedback.json")), "feedback_required"),
            (_fetch(load("err_checkpoint.json")), "checkpoint"),
            (_fetch({"message": "consent_required"}), "consent"),
            (_fetch(load("err_login_required.json")), "login_required"),
            (_fetch({"errors": [{"message": "Rate limit exceeded"}]}), "rate_limited"),
            (_fetch({}, url="https://www.instagram.com/challenge/x/"), "challenge"),
            (_fetch({}, url="https://www.instagram.com/accounts/suspended/"), "suspended"),
            (_fetch({}, url="https://www.instagram.com/accounts/login/?next=/"), "session_expired"),
            (guard.check_navigation("https://www.instagram.com/consent/x", 200), "consent"),
            (_fetch(None, text_head="<html>Please wait a few minutes</html>"), "rate_limited"),
        ]
        for signal, kind in cases:
            with self.subTest(kind=kind):
                self.assertIsNotNone(signal)
                self.assertEqual(signal.kind, kind)

    def test_caption_text_never_triggers(self) -> None:
        body = {"items": [{"caption": {"text": "Try again later, rate limit!"}}], "status": "ok"}
        self.assertIsNone(_fetch(body))

    def test_404_html_is_not_a_block(self) -> None:
        self.assertIsNone(_fetch(None, status=404, text_head='<html class="not-logged-in">'))


class TokenTests(unittest.TestCase):
    def test_extracts_all_forms_and_masks_repr(self) -> None:
        tokens = extract_tokens(text("html_tokens.html"))
        self.assertEqual(tokens.fb_dtsg, "FAKE:DTSG_TOKEN")
        self.assertEqual(tokens.lsd, "FAKE_LSD")
        self.assertEqual(tokens.actor_id, "17841400000000001")
        self.assertEqual(tokens.csrf_html, "FAKE_CSRF")
        self.assertEqual(tokens.client_revision, "1000000001")
        self.assertEqual(tokens.app_id, "111111111111111")
        self.assertEqual(tokens.jazoest, "2" + str(sum(map(ord, "FAKE:DTSG_TOKEN"))))
        self.assertNotIn("FAKE", repr(tokens))

    def test_fallback_pattern_and_missing(self) -> None:
        html = '"DTSGInitData",[],{"token":"T2"} "LSD",[],{"token":"L"} "actorID":"1"'
        self.assertEqual(extract_tokens(html).fb_dtsg, "T2")
        with self.assertRaises(TokenError):
            extract_tokens('"LSD",[],{"token":"L"}')


class OpsTableTests(unittest.TestCase):
    def test_table_integrity(self) -> None:
        friendly = set()
        for op in ops.TABLE.values():
            if isinstance(op, ops.GraphQLOp):
                self.assertTrue(op.doc_id.isdigit(), op.name)
                self.assertIn(op.endpoint, ops.ENDPOINTS)
                self.assertNotIn(op.friendly_name, friendly)
                friendly.add(op.friendly_name)
                self.assertEqual(op.root[0], "data")
                holes = ops.template_holes(ops._thaw(op.variables)) - ops.CLIENT_PARAMS
                ops.build_variables(op, {**{h: "x" for h in holes},
                                         **({"ig_did": "d"} if "ig_did" in ops.template_holes(ops._thaw(op.variables)) else {})})
            else:
                self.assertTrue(op.path.startswith("/api/v1/"))

    def test_template_errors(self) -> None:
        op = ops.TABLE["profile_info"]
        with self.assertRaises(ValueError):
            ops.build_variables(op, {})
        with self.assertRaises(ValueError):
            ops.build_variables(op, {"id": "1", "bogus": 2})


class PaginateTests(unittest.TestCase):
    def _pages(self, pages):
        calls = []

        async def fetch_page(upstream):
            calls.append(upstream)
            return pages[upstream]

        return fetch_page, calls

    def test_filtered_pages_do_not_cut_and_limit_resumes_mid_page(self) -> None:
        pages = {None: (["r1", "r2"], "c2"), "c2": (["r3", "p1", "p2", "p3"], None)}
        fetch_page, calls = self._pages(pages)
        page = asyncio.run(paginate(fetch_page, limit=2, max_pages=8, key_of=str,
                                    keep=lambda s: s.startswith("p")))
        self.assertEqual(page.items, ["p1", "p2"])
        self.assertEqual(page.next, CursorState("c2", 3))
        page2 = asyncio.run(paginate(fetch_page, limit=5, max_pages=8, key_of=str,
                                     start=page.next))
        self.assertEqual(page2.items, ["p3"])
        self.assertEqual(page2.stopped, "end")
        self.assertEqual(calls, [None, "c2", "c2"])

    def test_page_cap_and_budget(self) -> None:
        pages = {None: (["a"], "c2"), "c2": (["b"], "c3")}
        fetch_page, _ = self._pages(pages)
        page = asyncio.run(paginate(fetch_page, limit=9, max_pages=2, key_of=str))
        self.assertEqual((page.items, page.stopped, page.next.upstream), (["a", "b"], "page_cap", "c3"))

        async def budget(upstream):
            if upstream:
                raise BudgetExceededError("cap")
            return ["a"], "c2"

        page = asyncio.run(paginate(budget, limit=9, max_pages=5, key_of=str))
        self.assertEqual((page.items, page.stopped), (["a"], "budget"))


class PacerTests(unittest.TestCase):
    def test_pauses_and_cap(self) -> None:
        slept = []

        async def sleep(s):
            slept.append(s)

        pacer = Pacer(sleep=sleep, max_requests_per_call=2, uniform=lambda a, b: a)

        async def go():
            pacer.new_call()
            await pacer.before_request()
            await pacer.before_request()
            with self.assertRaises(BudgetExceededError):
                await pacer.before_request()

        asyncio.run(go())
        self.assertEqual(slept, [1.5, 3.0])


if __name__ == "__main__":
    unittest.main()
