"""Bulk downloads: manifest, incremental walks, resume, cancel, budget, streaming.

No network and no Chromium: a fake runtime hands a ``FakeClient`` (canned
feed pages) to each batch and a fake httpx client serves the CDN bytes.
Payloads are synthetic.

Run with: ``venv/bin/python -m unittest tests.test_bulk -v``
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeClient  # noqa: E402

from instagram_sdk import bulk, downloader  # noqa: E402
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    BudgetExceededError,
    OperationFailedError,
    StateUnavailableError,
)
from instagram_sdk.manifest import Manifest, manifest_path  # noqa: E402
from instagram_sdk.parsers import parse_post  # noqa: E402

CDN = "https://scontent-mad1-1.cdninstagram.com/v/t51/"


# --------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------
def _img(name: str) -> dict:
    return {"candidates": [{"url": f"{CDN}{name}.jpg?x=1", "width": 1080, "height": 1080}]}


def _node(pk: str, code: str, *, kind: str = "image", n: int = 2) -> dict:
    node: dict[str, Any] = {
        "pk": pk, "id": f"{pk}_70", "code": code, "taken_at": 1700000000,
        "user": {"pk": "70", "username": "someone", "full_name": "S",
                 "is_verified": False, "is_private": False},
    }
    if kind == "image":
        node.update(media_type=1, product_type="feed", image_versions2=_img(pk))
    elif kind == "carousel":
        node.update(media_type=8, product_type="carousel_container", carousel_media=[
            {"media_type": 1, "image_versions2": _img(f"{pk}_{i}")} for i in range(n)])
    elif kind == "video":
        node.update(media_type=2, product_type="clips", image_versions2=_img(pk),
                    video_versions=[{"url": f"{CDN}{pk}.mp4?x=1", "width": 720, "height": 1280}])
    elif kind == "video_nourl":
        node.update(media_type=2, product_type="clips", image_versions2=_img(pk))
    return node


def _saved_page(nodes: list[dict], nxt: str | None) -> dict:
    return {"items": [{"media": n} for n in nodes], "next_max_id": nxt,
            "more_available": nxt is not None}


def _grid_page(nodes: list[dict], nxt: str | None) -> dict:
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
        "edges": [{"node": n} for n in nodes],
        "page_info": {"end_cursor": nxt, "has_next_page": nxt is not None}}}}


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeStream:
    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "image/jpeg",
                 fail_after: int | None = None) -> None:
        self.status_code = status
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self._body = body
        self._fail_after = fail_after

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def aiter_bytes(self, chunk_size: int | None = None) -> Any:
        for i in range(0, len(self._body), 4):
            if self._fail_after is not None and i >= self._fail_after:
                raise ConnectionError("reset")
            yield self._body[i:i + 4]


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.overrides: dict[str, FakeStream] = {}

    def stream(self, method: str, url: str, **_: Any) -> FakeStream:
        self.calls.append(url)
        if url in self.overrides:
            return self.overrides[url]
        ctype = "video/mp4" if ".mp4" in url else "image/jpeg"
        return FakeStream(f"bytes-of:{url}".encode(), content_type=ctype)


class FakeBudget:
    def __init__(self, used: list[int]) -> None:
        self._used = used

    def used(self, cls: str, window_s: int) -> int:
        return self._used.pop(0) if len(self._used) > 1 else self._used[0]


class FakeState:
    def __init__(self) -> None:
        self.used_hour = [0]

    def budget(self, acct: Any, *, per_hour: int, per_day: int) -> FakeBudget:
        return FakeBudget(self.used_hour)

    def check(self, acct: Any) -> None:
        pass


class FakeRuntime:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.http = FakeHttp()
        self.state = FakeState()
        self.settings = SimpleNamespace(max_requests_per_hour=150, max_requests_per_day=800)
        self.runs = 0

    def resolve(self, account: Any) -> Any:
        return SimpleNamespace(handle=account or "carol", key=(account or "carol"))

    async def run(self, account: str, fn: Any) -> Any:
        self.runs += 1
        return await fn(self.client)

    def download_semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(2)

    def http_client(self) -> FakeHttp:
        return self.http


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class BulkTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "downloads"
        self.clock = Clock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def job(self, rt: FakeRuntime, source: str = "saved", target: str | None = None,
            **kw: Any) -> bulk.BulkJob:
        spec = bulk.make_spec(source, target, **kw)
        return bulk.BulkJob(rt, "carol", spec, downloads_dir=self.root,
                            sleep=self.clock.sleep, uniform=lambda a, b: a, clock=self.clock)

    def files(self) -> list[str]:
        folder = self.root / "carol"
        return sorted(p.name for p in folder.iterdir()
                      if p.is_file() and not p.name.startswith(".")
                      and p.name != "manifest.sqlite3") if folder.exists() else []


# --------------------------------------------------------------------------
class TestSpec(unittest.TestCase):
    def test_validation(self) -> None:
        self.assertEqual(bulk.make_spec("saved").source_key, "saved")
        self.assertEqual(bulk.make_spec("collection", "123").source_key, "collection:123")
        self.assertEqual(bulk.make_spec("user_posts", "@Some.One").source_key, "user:some.one")
        self.assertEqual(bulk.make_spec("collection", "ALL_MEDIA_AUTO_COLLECTION").source, "saved")
        for bad in (("nope", None), ("collection", None), ("collection", "abc"),
                    ("user_posts", None), ("user_posts", "bad name!")):
            with self.assertRaises(ValueError):
                bulk.make_spec(*bad)
        with self.assertRaises(ValueError):
            bulk.make_spec("saved", mode="fast")
        with self.assertRaises(ValueError):
            bulk.make_spec("saved", known_stop=0)
        self.assertEqual(bulk.make_spec("saved").job_id("Carol"), "carol:saved")
        self.assertEqual(bulk.parse_job_id("carol:collection:9"), ("carol", "collection:9"))
        with self.assertRaises(ValueError):
            bulk.parse_job_id("nojob")


class TestFirstAndSecondPass(BulkTestBase):
    def _client(self, pages: list[dict]) -> FakeClient:
        return FakeClient(rest={"saved_posts": pages})

    def test_full_then_incremental_downloads_nothing(self) -> None:
        p1 = _saved_page([_node("101", "AaA1"), _node("102", "AaA2", kind="carousel")], "P2")
        p2 = _saved_page([_node("103", "AaA3", kind="video")], None)
        rt = FakeRuntime(self._client([p1, p2]))
        out = _run(self.job(rt).run())
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["full_done"])
        self.assertEqual(out["this_run"]["downloaded"], 3)  # 1 image + 2 slides
        self.assertEqual(out["this_run"]["skipped"], 1)  # the video, not requested
        self.assertEqual(out["this_run"]["pages"], 2)
        self.assertEqual(self.files(), [
            "post_AaA1_101_00.jpg", "post_AaA2_102_00.jpg", "post_AaA2_102_01.jpg"])
        self.assertEqual(out["manifest_totals"]["done"], 3)
        self.assertEqual(len(rt.http.calls), 3)
        # Media URLs came from the feed: no post-page navigation at all.
        self.assertNotIn("html", [k for k, _, _ in rt.client.calls])
        # Paced between batches.
        self.assertGreaterEqual(sum(self.clock.slept), bulk.BATCH_PAUSE_S[0])

        # Second pass: same feed, everything known → stops at known_stop, no bytes.
        rt2 = FakeRuntime(self._client([p1, p2]))
        out2 = _run(self.job(rt2, known_stop=2).run())
        self.assertEqual(out2["status"], "done")
        self.assertEqual(out2["this_run"]["downloaded"], 0)
        self.assertEqual(out2["this_run"]["posts_known"], 2)
        self.assertEqual(out2["this_run"]["pages"], 1)
        self.assertEqual(rt2.http.calls, [])
        self.assertEqual(rt2.client.ops_called(), ["saved_posts"])

    def test_incremental_fetches_only_new_head(self) -> None:
        old = [_node("101", "AaA1"), _node("102", "AaA2")]
        rt = FakeRuntime(self._client([_saved_page(old, None)]))
        _run(self.job(rt).run())
        new = _saved_page([_node("200", "NeW1"), *old], None)
        rt2 = FakeRuntime(self._client([new]))
        out = _run(self.job(rt2, known_stop=2).run())
        self.assertEqual(out["this_run"]["downloaded"], 1)
        self.assertEqual(rt2.http.calls, [f"{CDN}200.jpg?x=1"])
        self.assertEqual(out["manifest_totals"]["posts"], 3)

    def test_written_files_match_manifest(self) -> None:
        rt = FakeRuntime(self._client([_saved_page([_node("101", "AaA1")], None)]))
        _run(self.job(rt).run())
        with Manifest(manifest_path(self.root, "carol"), account="carol") as m:
            (entry,) = m.entries()
        data = Path(entry.saved_path).read_bytes()
        self.assertEqual(entry.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(entry.byte_size, len(data))
        self.assertEqual((entry.pk, entry.index, entry.status), ("101", 0, "done"))


class TestResume(BulkTestBase):
    def test_partial_then_resume_from_cursor(self) -> None:
        p1 = _saved_page([_node("101", "AaA1"), _node("102", "AaA2")], "P2")
        p2 = _saved_page([_node("103", "AaA3")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1]}))
        out = _run(self.job(rt, max_pages=1).run())
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["resumable"])
        self.assertFalse(out["full_done"])

        rt2 = FakeRuntime(FakeClient(rest={"saved_posts": [p1, p2]}))
        out2 = _run(self.job(rt2, known_stop=2).run())
        self.assertEqual(out2["status"], "done")
        self.assertTrue(out2["full_done"])
        self.assertEqual(out2["this_run"]["downloaded"], 1)
        queries = [c[2]["query"].get("max_id") for c in rt2.client.calls if c[0] == "rest"]
        self.assertEqual(queries, [None, "P2"])  # head scan, then resume at the cursor

    def test_max_posts_mid_page_keeps_page_cursor(self) -> None:
        p1 = _saved_page([_node("101", "AaA1"), _node("102", "AaA2")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1]}))
        out = _run(self.job(rt, max_posts=1).run())
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["this_run"]["downloaded"], 1)
        rt2 = FakeRuntime(FakeClient(rest={"saved_posts": [p1]}))
        out2 = _run(self.job(rt2).run())  # head scan reaches the end of the feed
        self.assertEqual(out2["status"], "done")
        self.assertEqual(out2["this_run"]["downloaded"], 1)

    def test_expired_resume_cursor_restarts_from_top(self) -> None:
        p1 = _saved_page([_node("101", "AaA1")], "P2")
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1]}))
        _run(self.job(rt, max_pages=1).run())
        p1b = _saved_page([_node("101", "AaA1")], "P2b")
        p2 = _saved_page([_node("103", "AaA3")], None)
        stale = OperationFailedError("saved_posts", "bad max_id")
        rt2 = FakeRuntime(FakeClient(rest={"saved_posts": [p1, stale, p1b, p2]}))
        out = _run(self.job(rt2, known_stop=1).run())
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["this_run"]["downloaded"], 1)

    def test_interrupted_job_is_reported_and_resumable(self) -> None:
        rt = FakeRuntime(FakeClient(rest={"saved_posts": []}))
        job = self.job(rt)
        job.claim()
        job._release()  # process "died" with the job marked running
        status = bulk.job_status(self.root, "carol:saved")
        self.assertEqual(status["status"], "interrupted")
        self.assertFalse(status["running"])


class TestVideos(BulkTestBase):
    def test_videos_streamed_when_requested_later(self) -> None:
        page = _saved_page([_node("103", "AaA3", kind="video")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [page]}))
        out = _run(self.job(rt).run())
        self.assertEqual(out["this_run"]["skipped"], 1)
        self.assertEqual(self.files(), [])
        rt2 = FakeRuntime(FakeClient(rest={"saved_posts": [page]}))
        out2 = _run(self.job(rt2, download_videos=True).run())
        self.assertEqual(out2["this_run"]["downloaded"], 1)
        self.assertEqual(self.files(), ["post_AaA3_103_00.mp4"])

    def test_video_without_url_resolved_from_post_page(self) -> None:
        page = _saved_page([_node("104", "AaA4", kind="video_nourl")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [page]}))
        resolved = mock.AsyncMock(return_value=parse_post(
            _node("104", "AaA4", kind="video")))
        with mock.patch.object(bulk.media_ep, "resolve_post_for_download", resolved):
            out = _run(self.job(rt, download_videos=True).run())
        resolved.assert_awaited_once()
        self.assertEqual(out["this_run"]["fallback_resolves"], 1)
        self.assertEqual(self.files(), ["post_AaA4_104_00.mp4"])


class TestControl(BulkTestBase):
    def test_one_job_per_account(self) -> None:
        rt = FakeRuntime(FakeClient())
        first = self.job(rt)
        first.claim()
        try:
            with self.assertRaises(bulk.BulkJobBusyError):
                self.job(rt, "collection", "5").claim()
            self.assertTrue(bulk.job_status(self.root, "carol:saved")["running"])
        finally:
            first._release()

    def test_cancel_from_outside_stops_the_job(self) -> None:
        p1 = _saved_page([_node("101", "AaA1")], "P2")
        p2 = _saved_page([_node("103", "AaA3")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1, p2]}))
        job = self.job(rt)
        job.claim()
        real_sleep = self.clock.sleep

        async def sleep_and_cancel(s: float) -> None:
            bulk.request_cancel(self.root, job.job_id)  # e.g. another process
            await real_sleep(s)

        job._sleep = sleep_and_cancel
        out = _run(job.run())
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["this_run"]["pages"], 1)
        self.assertTrue(out["resumable"])

    def test_cancel_of_idle_job_marks_it(self) -> None:
        rt = FakeRuntime(FakeClient())
        job = self.job(rt)
        job.claim()
        job._release()
        self.assertEqual(bulk.request_cancel(self.root, "carol:saved")["status"], "cancelled")
        self.assertIsNone(bulk.request_cancel(self.root, "carol:user:x"))

    def test_block_signal_ends_job_without_retry(self) -> None:
        err = AccountBlockedError("carol", "rate_limited", "Please wait", until=None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [err]}))
        out = _run(self.job(rt).run())
        self.assertEqual(out["status"], "error")
        self.assertIn("rate_limited", out["error"])
        self.assertEqual(rt.runs, 1)

    def test_budget_error_stops_partial_keeping_cursor(self) -> None:
        p1 = _saved_page([_node("101", "AaA1")], "P2")
        cut = BudgetExceededError("hourly budget of 150 reached", retry_after_s=600,
                                  partial=[], next=None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1, cut]}))
        out = _run(self.job(rt).run())
        self.assertEqual(out["status"], "partial")
        self.assertIn("budget", out["error"])
        self.assertIn("600", out["error"])
        self.assertTrue(out["resumable"])
        self.assertEqual(rt.runs, 2)  # no retry loop after the cut
        # Resuming continues at P2, not from the top.
        p2 = _saved_page([_node("103", "AaA3")], None)
        rt2 = FakeRuntime(FakeClient(rest={"saved_posts": [p1, p2]}))
        out2 = _run(self.job(rt2, known_stop=1).run())
        self.assertEqual(out2["status"], "done")
        queries = [c[2]["query"].get("max_id") for c in rt2.client.calls if c[0] == "rest"]
        self.assertEqual(queries, [None, "P2"])

    def test_state_unavailable_is_error_and_keeps_cursor(self) -> None:
        p1 = _saved_page([_node("101", "AaA1")], "P2")
        err = StateUnavailableError("cannot write state")
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1, err]}))
        out = _run(self.job(rt).run())
        self.assertEqual(out["status"], "error")
        self.assertIn("cannot write state", out["error"])
        self.assertTrue(out["resumable"])
        self.assertEqual(rt.runs, 2)

    def test_budget_cut_during_video_fallback_keeps_page(self) -> None:
        page = _saved_page([_node("104", "AaA4", kind="video_nourl"),
                            _node("105", "AaA5")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [page]}))
        cut = mock.AsyncMock(side_effect=BudgetExceededError("cap", retry_after_s=60))
        with mock.patch.object(bulk.media_ep, "resolve_post_for_download", cut):
            out = _run(self.job(rt, download_videos=True).run())
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.files(), ["post_AaA5_105_00.jpg"])
        self.assertEqual(out["this_run"]["failed"], 1)  # video retried on a later run

    def test_waits_while_budget_is_in_reserve(self) -> None:
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [_saved_page([], None)]}))
        rt.state.used_hour = [150 - bulk.HOURLY_RESERVE, 0]
        out = _run(self.job(rt).run())
        self.assertEqual(out["status"], "done")
        self.assertAlmostEqual(sum(self.clock.slept), bulk.BUDGET_RECHECK_S)

    def test_existing_file_is_adopted_not_refetched(self) -> None:
        folder = self.root / "carol"
        folder.mkdir(parents=True)
        (folder / "post_AaA1_101_00.jpg").write_bytes(b"from download_media")
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [
            _saved_page([_node("101", "AaA1")], None)]}))
        out = _run(self.job(rt).run())
        self.assertEqual(out["this_run"]["adopted"], 1)
        self.assertEqual(rt.http.calls, [])


class TestManifest(BulkTestBase):
    def test_corrupt_manifest_is_set_aside(self) -> None:
        path = manifest_path(self.root, "carol")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"this is not sqlite" * 100)
        with Manifest(path, account="carol") as m:
            self.assertEqual(m.totals()["posts"], 0)
        self.assertEqual(len(list(path.parent.glob("manifest.sqlite3.corrupt-*"))), 1)

    def test_known_rules(self) -> None:
        with Manifest(manifest_path(self.root, "carol"), account="carol") as m:
            base = dict(pk="1", shortcode="A", kind="video")
            m.record(idx=0, status="skipped", reason="video_not_requested", **base)
            self.assertTrue(m.is_known("1", 1, download_videos=False))
            self.assertFalse(m.is_known("1", 1, download_videos=True))
            for _ in range(3):
                m.record(idx=0, status="failed", reason="http_404", **base)
            self.assertTrue(m.is_known("1", 1, download_videos=True))  # given up
            self.assertFalse(m.is_known("1", 2, download_videos=False))  # leaf 1 missing


class TestUserPosts(BulkTestBase):
    def test_profile_grid_pages_and_reels(self) -> None:
        g1 = _grid_page([_node("301", "GgG1"), _node("302", "GgG2", kind="video")], "C2")
        g2 = _grid_page([_node("303", "GgG3")], None)
        client = FakeClient(graphql={"profile_posts_first": [g1], "profile_posts_page": [g2]})
        rt = FakeRuntime(client)
        out = _run(self.job(rt, "user_posts", "someone").run())
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["this_run"]["downloaded"], 2)
        self.assertEqual(out["this_run"]["skipped"], 1)
        # No reels-tab lookups: only the two grid pages.
        self.assertEqual(client.ops_called(), ["profile_posts_first", "profile_posts_page"])


class TestStreamToFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stream(self, stream: FakeStream, max_bytes: int | None = None) -> Any:
        http = FakeHttp()
        http.overrides[f"{CDN}a.jpg"] = stream
        return _run(downloader.stream_to_file(
            http, f"{CDN}a.jpg", path_for_ext=lambda ext: self.dir / f"a.{ext}",
            max_bytes=max_bytes))

    def test_ok_hashes_and_renames(self) -> None:
        out = self._stream(FakeStream(b"0123456789", content_type="image/png"))
        self.assertTrue(out.ok)
        self.assertEqual(out.path, self.dir / "a.png")
        self.assertEqual(out.sha256, hashlib.sha256(b"0123456789").hexdigest())
        self.assertEqual(os.listdir(self.dir), ["a.png"])

    def test_failure_mid_stream_leaves_nothing(self) -> None:
        out = self._stream(FakeStream(b"0123456789", fail_after=4))
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, downloader.REASON_DOWNLOAD_ERROR)
        self.assertEqual(os.listdir(self.dir), [])

    def test_too_large(self) -> None:
        out = self._stream(FakeStream(b"0123456789"), max_bytes=5)
        self.assertEqual(out.reason, downloader.REASON_TOO_LARGE)
        self.assertEqual(os.listdir(self.dir), [])

    def test_404(self) -> None:
        out = self._stream(FakeStream(b"", status=404))
        self.assertEqual(out.reason, downloader.REASON_HTTP_404)


class TestServerTools(BulkTestBase):
    def test_start_status_cancel(self) -> None:
        from instagram_sdk import server as server_mod

        p1 = _saved_page([_node("101", "AaA1")], None)
        rt = FakeRuntime(FakeClient(rest={"saved_posts": [p1]}))
        rt.registry = SimpleNamespace(list=lambda: [SimpleNamespace(handle="carol")])

        async def scenario() -> tuple[dict, dict, dict]:
            started = await server_mod.start_bulk_download("saved", account="carol")
            with self.assertRaises(ValueError):  # one job per account
                await server_mod.start_bulk_download("collection", "5", account="carol")
            await asyncio.gather(*list(server_mod._bulk_tasks.values()))
            status = await server_mod.bulk_download_status()
            cancelled = await server_mod.cancel_bulk_download(started["job_id"])
            return started, status, cancelled

        with mock.patch.object(server_mod, "_runtime", rt), \
                mock.patch.dict(os.environ, {"INSTAGRAM_DOWNLOADS_DIR": str(self.root)}), \
                mock.patch.object(bulk, "BATCH_PAUSE_S", (0.0, 0.0)):
            started, status, cancelled = _run(scenario())
        self.assertEqual(started["job_id"], "carol:saved")
        self.assertTrue(started["running"])
        (job,) = status["jobs"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["this_run"]["downloaded"], 1)
        self.assertNotIn("cursor", job)
        self.assertEqual(cancelled["status"], "done")  # finished jobs stay finished


if __name__ == "__main__":
    unittest.main()
