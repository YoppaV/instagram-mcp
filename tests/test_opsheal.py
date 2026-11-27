"""Self-heal of doc_id/variables: pure helpers, the cache and the retry flow.

No network, no Chromium: the heal page is a fake that "sends" the web's own
GraphQL POSTs when it navigates.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeContext, FakePage, FakeResponse, fetch_result, load, text  # noqa: E402

from instagram_sdk import ops, opsheal  # noqa: E402
from instagram_sdk.browser import AccountContext  # noqa: E402
from instagram_sdk.errors import AccountBlockedError, DocIdStaleError  # noqa: E402
from instagram_sdk.inpage import InPageClient  # noqa: E402
from instagram_sdk.opsheal import Healer, OpsStore  # noqa: E402
from instagram_sdk.pacer import Pacer  # noqa: E402

NEW_DOC = "11112222333344445"
STALE = load("err_gql_stale.json")
POSTS = load("gql_profile_posts.json")


async def _no_sleep(_s: float) -> None:
    return None


def _run(coro):
    return asyncio.run(coro)


def _post(friendly: str, doc_id: str, variables: dict) -> str:
    return urlencode({"fb_api_req_friendly_name": friendly, "doc_id": doc_id,
                      "variables": json.dumps(variables), "fb_dtsg": "X"})


class _Req:
    def __init__(self, url: str, post_data: str | None, rtype: str = "fetch") -> None:
        self.url = url
        self.method = "POST" if post_data is not None else "GET"
        self.post_data = post_data
        self.resource_type = rtype


class _Route:
    def __init__(self, req: _Req) -> None:
        self.request = req
        self.aborted = False

    async def abort(self):
        self.aborted = True

    async def fallback(self):
        return None


class _Resp:
    def __init__(self, req: _Req, status: int, body: str) -> None:
        self.url = req.url
        self.request = req
        self.status = status
        self.headers = {"content-type": "text/javascript"}
        self._body = body

    async def text(self) -> str:
        return self._body


class HealPage:
    """Second tab: on goto it fires the configured requests/responses."""

    def __init__(self, posts: list[str], *, scripts: list[str] = (), xhr_body: str = "{}",
                 xhr_status: int = 200, final_url: str | None = None, html: str = "",
                 extra: list[str] = ()) -> None:
        self._posts = posts
        self._extra = list(extra)
        self._scripts = list(scripts)
        self._xhr_body = xhr_body
        self._xhr_status = xhr_status
        self._final = final_url
        self._html = html
        self._handlers: dict[str, list] = {}
        self.url = "about:blank"
        self.gotos: list[str] = []
        self.closed = False
        self.aborted: list[str] = []
        self._route = None

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    async def route(self, _pattern, handler):
        self._route = handler

    async def goto(self, url, **_):
        self.gotos.append(url)
        self.url = self._final or url
        for body in self._posts:
            req = _Req("https://www.instagram.com/api/graphql", body)
            route = _Route(req)
            await self._route(route)
            if route.aborted:
                self.aborted.append(parse_qs(body)["fb_api_req_friendly_name"][0])
                continue
            for fn in self._handlers.get("response", []):
                fn(_Resp(req, self._xhr_status, self._xhr_body))
        for url in self._extra:
            route = _Route(_Req(url, None, "xhr"))
            await self._route(route)
            if route.aborted:
                self.aborted.append(url)
        for js in self._scripts:
            req = _Req("https://static.cdninstagram.com/rsrc.php/x.js", None, "script")
            route = _Route(req)
            await self._route(route)
            assert not route.aborted, "CDN scripts must load"
            for fn in self._handlers.get("response", []):
                fn(_Resp(req, 200, js))
        await asyncio.sleep(0)
        return FakeResponse(200)

    async def evaluate(self, *_a):
        return None

    async def content(self):
        return self._html

    async def close(self):
        self.closed = True


class HealContext(FakeContext):
    def __init__(self, heal_page: HealPage | None) -> None:
        super().__init__()
        self.heal_page = heal_page
        self.new_pages = 0

    async def new_page(self):
        self.new_pages += 1
        if self.heal_page is None:
            raise AssertionError("no heal page expected")
        return self.heal_page

    async def new_cdp_session(self, _page):
        class _C:
            async def send(self, *_a):
                return None
        return _C()


def _client(results, heal_page=None, *, tmp: Path, enabled=True):
    page = FakePage(results, html=text("html_tokens.html"))
    ctx = HealContext(heal_page)
    actx = AccountContext(handle="carol", context=ctx, page=page, session_fp=None,
                          pacer=Pacer(sleep=_no_sleep), ua_override={"userAgent": "UA"})
    blocks = []

    async def on_block(signal):
        blocks.append(signal)
        return AccountBlockedError("carol", signal.kind, signal.detail)

    store = OpsStore(tmp / "ops_cache.json")
    healer = Healer(store, enabled=enabled, debug_dir=tmp / "debug", sleep=_no_sleep,
                    uniform=lambda a, b: a, wait_s=1.0)
    client = InPageClient(actx, on_block=on_block, healer=healer)
    client.new_call()
    return client, page, ctx, healer, blocks


def _sent_doc_ids(page: FakePage) -> list[str]:
    return [parse_qs(f["body"])["doc_id"][0] for f in page.fetches]


class PureHelperTests(unittest.TestCase):
    def test_preloaded_query_ids(self) -> None:
        html = ('..{"preloaderID":"adp_PolarisProfilePostsQueryRelayPreloader_6a1b_0",'
                '"queryID":"123456789012345","variables":{}}..'
                'adp_OtherQueryRelayPreloader_9 nothing here')
        self.assertEqual(opsheal.extract_preloaded_query_ids(html),
                         {"PolarisProfilePostsQuery": "123456789012345"})
        self.assertEqual(opsheal.extract_preloaded_query_ids("<html></html>"), {})

    def test_bundle_doc_ids(self) -> None:
        js = ('__d("PolarisSearchBoxRefetchableQuery_instagramRelayOperation",[],'
              '(function(a,b,c,d,e,f){e.exports="99998888777766665"}),null);')
        self.assertEqual(opsheal.extract_bundle_doc_ids(js),
                         {"PolarisSearchBoxRefetchableQuery": "99998888777766665"})

    def test_heal_tab_request_filter(self) -> None:
        self.assertTrue(opsheal.is_instagram_host("www.instagram.com"))
        self.assertFalse(opsheal.is_instagram_host("static.cdninstagram.com"))
        allowed = opsheal.heal_request_allowed
        self.assertTrue(allowed("document", "/carol/"))
        self.assertTrue(allowed("fetch", "/api/graphql"))
        self.assertTrue(allowed("xhr", "/graphql/query"))
        self.assertFalse(allowed("xhr", "/ajax/bz"))
        self.assertFalse(allowed("fetch", "/api/v1/web/whatever/"))
        self.assertTrue(allowed("fetch", "/api/v1/web/whatever/", opsheal.ABORT_LOGGING))
        self.assertFalse(allowed("xhr", "/ajax/bz", opsheal.ABORT_LOGGING))

    def test_parse_graphql_post(self) -> None:
        got = opsheal.parse_graphql_post(_post("Q", "12345678", {"a": 1}))
        self.assertEqual(got, ("Q", "12345678", {"a": 1}))
        self.assertIsNone(opsheal.parse_graphql_post("doc_id=abc"))

    def test_retemplate_restores_holes_and_adopts_new_flags(self) -> None:
        template = {"username": "$username", "after": "$after?", "data": {"id": "$id", "n": 12},
                    "flag": False}
        captured = {"username": "someone", "data": {"id": "42", "n": 24, "new": 1},
                    "flag": True, "__relay_internal__pv__X": True}
        self.assertEqual(
            opsheal.retemplate(template, captured),
            {"username": "$username", "data": {"id": "$id", "n": 24, "new": 1}, "flag": True,
             "__relay_internal__pv__X": True},
        )

    def test_retemplate_drops_another_variant(self) -> None:
        self.assertIsNone(opsheal.retemplate({"username": "$username"}, {"id": "1"}))
        self.assertIsNone(opsheal.retemplate({"data": {"id": "$id"}}, {"x": 1}))


class OpsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "ops_cache.json"
        self.op = ops.TABLE["profile_posts_first"]

    def test_update_and_overlay(self) -> None:
        store = OpsStore(self.path)
        self.assertIs(store.get(self.op), self.op)
        variables = {"data": {"count": 12}, "username": "someone", "newflag": True}
        self.assertTrue(store.update(self.op.friendly_name, doc_id=NEW_DOC,
                                     variables=variables, source="heal:xhr"))
        got = OpsStore(self.path).get(self.op)
        self.assertEqual(got.doc_id, NEW_DOC)
        self.assertEqual(ops.build_variables(got, {"username": "carol"}),
                         {"data": {"count": 12}, "username": "carol", "newflag": True})
        # Same capture again: nothing changed.
        self.assertFalse(store.update(self.op.friendly_name, doc_id=NEW_DOC,
                                      variables=variables, source="heal:xhr"))
        # Unknown op names are not stored.
        self.assertFalse(store.update("NotOurs", doc_id=NEW_DOC, variables=None, source="x"))

    def test_older_than_table_is_ignored(self) -> None:
        self.path.write_text(json.dumps({"version": 1, "ops": {self.op.friendly_name: {
            "doc_id": NEW_DOC, "updated_at": "2020-01-01T00:00:00Z"}}}))
        self.assertEqual(OpsStore(self.path).get(self.op).doc_id, self.op.doc_id)

    def test_corrupt_cache_is_ignored(self) -> None:
        self.path.write_text("{not json")
        with self.assertLogs("instagram_sdk", "WARNING"):
            self.assertIs(OpsStore(self.path).get(self.op), self.op)

    def test_cached_variables_with_wrong_holes_fall_back_to_repo_template(self) -> None:
        self.path.write_text(json.dumps({"version": 1, "ops": {self.op.friendly_name: {
            "doc_id": NEW_DOC, "updated_at": "2030-01-01T00:00:00Z",
            "variables": {"username": "$other"}}}}))
        got = OpsStore(self.path).get(self.op)
        self.assertEqual(got.doc_id, NEW_DOC)
        self.assertEqual(got.variables, self.op.variables)

    def test_limits(self) -> None:
        now = [1_000_000.0]
        store = OpsStore(self.path, clock=lambda: now[0])
        self.assertIsNone(store.heal_refusal("a", "carol"))
        store.record_heal("a", "carol")
        self.assertIn("6 h", store.heal_refusal("a", "carol"))
        store.record_heal("b", "carol")
        self.assertIn("today", store.heal_refusal("c", "carol"))
        self.assertIsNone(store.heal_refusal("c", "dave"))
        now[0] += 86401
        self.assertIsNone(store.heal_refusal("a", "carol"))


class HealFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.friendly = ops.TABLE["profile_posts_first"].friendly_name
        self.captured = {"data": {"count": 12}, "username": "carol", "extra": True}

    def test_stale_heals_from_the_web_request_and_retries_once(self) -> None:
        heal = HealPage([_post(self.friendly, NEW_DOC, self.captured),
                         _post("PolarisProfilePageContentQuery", "55556666777788889",
                               {"id": "1", "enable_integrity_filters": True})],
                        extra=["https://www.instagram.com/ajax/bz?x=1"])
        client, page, ctx, healer, _ = _client([fetch_result(STALE), fetch_result(POSTS)],
                                               heal, tmp=self.tmp)
        root = _run(client.graphql("profile_posts_first", username="carol"))
        self.assertIsNotNone(root)
        self.assertEqual(heal.gotos, ["https://www.instagram.com/carol/"])
        self.assertTrue(heal.closed)
        # The healed op never reaches Instagram from the heal page; others do.
        self.assertEqual(heal.aborted, [self.friendly, "https://www.instagram.com/ajax/bz?x=1"])
        self.assertEqual(healer.last_sent, 1)
        self.assertEqual(_sent_doc_ids(page), [ops.TABLE["profile_posts_first"].doc_id, NEW_DOC])
        retry_vars = json.loads(parse_qs(page.fetches[1]["body"])["variables"][0])
        self.assertTrue(retry_vars["extra"])
        self.assertEqual(healer.last_outcome, opsheal.HEALED)
        # The other op seen on the page was refreshed for free.
        cache = json.loads((self.tmp / "ops_cache.json").read_text())
        self.assertEqual(cache["ops"]["PolarisProfilePageContentQuery"]["doc_id"],
                         "55556666777788889")
        # 1 nav + 1 send + HEAL_COST + 1 retry, inside the per-call cap even
        # with a token refresh on top.
        self.assertEqual(client.pacer.requests_this_call, 3 + opsheal.HEAL_COST)
        from instagram_sdk.pacer import MAX_REQUESTS_PER_CALL
        self.assertLessEqual(4 + opsheal.HEAL_COST, MAX_REQUESTS_PER_CALL)
        # Next call uses the cured doc_id straight away.
        client.new_call()
        page.results.append(fetch_result(POSTS))
        _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(_sent_doc_ids(page)[-1], NEW_DOC)

    def test_real_doc_not_found_shape_heals_without_token_refresh(self) -> None:
        from unittest import mock

        from instagram_sdk import inpage

        body = load("err_gql_doc_not_found.json")
        self.assertTrue(opsheal.is_unknown_doc_id(body))
        self.assertFalse(opsheal.is_unknown_doc_id(STALE))
        op = ops.TABLE["profile_posts_first"]
        self.assertEqual(inpage.classify_graphql(200, body, op)[0], inpage.STALE)
        heal = HealPage([_post(self.friendly, NEW_DOC, self.captured)])
        client, page, _, _, _ = _client([fetch_result(body), fetch_result(POSTS)], heal,
                                        tmp=self.tmp)
        # Tokens count as old: a generic STALE would refresh them first.
        with mock.patch.object(inpage, "TOKEN_REFRESH_MIN_AGE_S", -1):
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(page.gotos, ["https://www.instagram.com/"])  # bootstrap only
        self.assertEqual(_sent_doc_ids(page), [op.doc_id, NEW_DOC])

    def test_same_doc_id_means_shape_changed_no_retry(self) -> None:
        doc = ops.TABLE["profile_posts_first"].doc_id
        same = {**ops._thaw(ops.TABLE["profile_posts_first"].variables), "username": "x"}
        heal = HealPage([_post(self.friendly, doc, same)])
        client, page, _, _, _ = _client([fetch_result(STALE)], heal, tmp=self.tmp)
        with self.assertRaises(DocIdStaleError) as cm:
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertIn("changed shape", str(cm.exception))
        self.assertEqual(len(page.fetches), 1)
        self.assertEqual(len(list((self.tmp / "debug").glob("*.json"))), 1)

    def test_bundle_regex_when_the_web_does_not_send_the_op(self) -> None:
        js = (f'__d("{self.friendly}_instagramRelayOperation",[],'
              f'(function(a,b,c,d,e,f){{e.exports="{NEW_DOC}"}}),null);')
        client, page, _, _, _ = _client([fetch_result(STALE), fetch_result(POSTS)],
                                        HealPage([], scripts=[js]), tmp=self.tmp)
        _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(_sent_doc_ids(page)[-1], NEW_DOC)

    def test_block_signal_on_heal_page_stops_without_retry(self) -> None:
        heal = HealPage([_post("PolarisProfilePageContentQuery", "55556666777788889", {"id": "1"}),
                         _post(self.friendly, NEW_DOC, self.captured)],
                        xhr_body=json.dumps({"message": "Please wait a few minutes before you try again."}),
                        xhr_status=200)
        client, page, _, _, blocks = _client([fetch_result(STALE)], heal, tmp=self.tmp)
        with self.assertRaises(AccountBlockedError):
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(len(page.fetches), 1)
        self.assertEqual(blocks[0].kind, "rate_limited")

    def test_challenge_redirect_on_heal_page_blocks(self) -> None:
        heal = HealPage([], final_url="https://www.instagram.com/challenge/abc/")
        client, page, _, _, blocks = _client([fetch_result(STALE)], heal, tmp=self.tmp)
        with self.assertRaises(AccountBlockedError):
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(blocks[0].kind, "challenge")

    def test_block_signal_in_the_answer_never_heals(self) -> None:
        client, page, ctx, _, _ = _client([fetch_result(load("err_wait.json"))], None,
                                          tmp=self.tmp)
        with self.assertRaises(AccountBlockedError):
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(ctx.new_pages, 0)

    def test_disabled_and_limited(self) -> None:
        client, _, ctx, _, _ = _client([fetch_result(STALE)], None, tmp=self.tmp, enabled=False)
        with self.assertRaises(DocIdStaleError):
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(ctx.new_pages, 0)
        store = OpsStore(self.tmp / "ops_cache.json")
        store.record_heal("profile_posts_first", "carol")
        client, _, ctx, _, _ = _client([fetch_result(STALE)], None, tmp=self.tmp)
        with self.assertRaises(DocIdStaleError):
            _run(client.graphql("profile_posts_first", username="carol"))
        self.assertEqual(ctx.new_pages, 0)

    def test_reels_path_uses_the_cached_username(self) -> None:
        friendly = ops.TABLE["profile_reels_first"].friendly_name
        cap = {**ops._thaw(ops.TABLE["profile_reels_first"].variables), "user_id": "42"}
        cap["data"] = {**cap["data"], "target_user_id": "42"}
        heal = HealPage([_post(friendly, NEW_DOC, cap)])
        client, page, _, _, _ = _client([fetch_result(STALE), fetch_result(load("gql_reels.json"))],
                                        heal, tmp=self.tmp)
        from instagram_sdk.endpoints.users import UserIdCache
        client.user_ids = UserIdCache(None)
        client.user_ids.put("carol", "42")
        _run(client.graphql("profile_reels_first", id="42"))
        self.assertEqual(heal.gotos, ["https://www.instagram.com/carol/reels/"])


class LoggingTests(unittest.TestCase):
    def test_http_libraries_do_not_log_signed_urls(self) -> None:
        import logging

        from instagram_sdk import server  # noqa: F401

        for name in ("httpx", "httpcore"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)
        self.assertGreaterEqual(logging.getLogger().level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
