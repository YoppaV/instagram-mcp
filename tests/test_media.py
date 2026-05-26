"""Tests for the ``download_media`` tool and the ``resolve_post_media`` resolver.

The browser and the underlying ``post.fetch`` are mocked, and ``fetch_bytes`` is
patched with canned outcomes, so nothing touches the network or a real browser.
Saved files go to a temp directory.

Run with: ``venv/bin/python -m unittest tests.test_media -v``
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mcp.server.fastmcp import Image  # noqa: E402

from instagram_sdk import server as server_mod  # noqa: E402
from instagram_sdk.downloader import FetchOutcome  # noqa: E402
from instagram_sdk.endpoints import media as media_mod  # noqa: E402
from instagram_sdk.models import MediaItem, Post, User  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TINY_JPEG = (FIXTURES / "tiny.jpg").read_bytes()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _image_item() -> MediaItem:
    return MediaItem(
        media_type=1,
        image_url="https://scontent-mad1-1.cdninstagram.com/v/t51/p.jpg?x=1",
        video_url=None,
        width=1080,
        height=1080,
    )


def _video_item() -> MediaItem:
    return MediaItem(
        media_type=2,
        image_url="https://scontent-mad1-1.cdninstagram.com/v/t51/thumb.jpg",
        video_url="https://scontent-mad2-1.cdninstagram.com/o1/v/t2/v.mp4?y=2",
        width=640,
        height=1136,
    )


class FakeBrowser:
    @asynccontextmanager
    async def page(self) -> Any:
        yield object()  # opaque — the resolver is mocked, never touches it


def _ok(content_type: str, data: bytes) -> FetchOutcome:
    return FetchOutcome(ok=True, data=data, content_type=content_type, reason=None)


def _fail(reason: str) -> FetchOutcome:
    return FetchOutcome(ok=False, data=None, content_type="", reason=reason)


class DownloadMediaToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _call(
        self,
        items: tuple[MediaItem, ...],
        *,
        outcomes: list[FetchOutcome],
        indices: list[int] | None = None,
        download_videos: bool = False,
        shortcode: str = "DYdWoL8gASP",
    ) -> list[Any]:
        resolve = mock.AsyncMock(return_value=(shortcode, items))
        fetch = mock.AsyncMock(side_effect=outcomes)
        with mock.patch.object(server_mod, "_browser", FakeBrowser()), \
             mock.patch.object(server_mod, "_DOWNLOADS_DIR", self.tmp), \
             mock.patch.object(server_mod.media, "resolve_post_media", new=resolve), \
             mock.patch.object(server_mod.downloader, "fetch_bytes", new=fetch):
            return _run(
                server_mod.download_media(
                    id_or_url=shortcode,
                    indices=indices,
                    download_videos=download_videos,
                )
            )

    def test_image_is_downloaded_and_returned_inline(self) -> None:
        result = self._call(
            (_image_item(),), outcomes=[_ok("image/jpeg", TINY_JPEG)]
        )
        images = [r for r in result if isinstance(r, Image)]
        summary = result[-1]
        self.assertEqual(len(images), 1)
        self.assertEqual(len(summary["downloaded"]), 1)
        self.assertEqual(summary["downloaded"][0]["kind"], "image")
        self.assertEqual(len(summary["skipped"]), 0)
        saved = Path(summary["downloaded"][0]["saved_path"])
        self.assertTrue(saved.exists())
        self.assertEqual(saved.name, "post_DYdWoL8gASP_00.jpg")
        self.assertEqual(saved.read_bytes(), TINY_JPEG)

    def test_video_skipped_unless_opted_in(self) -> None:
        result = self._call((_video_item(),), outcomes=[])
        summary = result[-1]
        self.assertEqual(len(summary["downloaded"]), 0)
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertEqual(summary["skipped"][0]["reason"], "video_not_requested")
        self.assertEqual([r for r in result if isinstance(r, Image)], [])

    def test_video_downloaded_when_opted_in_but_not_inline(self) -> None:
        result = self._call(
            (_video_item(),),
            outcomes=[_ok("video/mp4", b"\x00\x00mp4")],
            download_videos=True,
        )
        summary = result[-1]
        self.assertEqual(len(summary["downloaded"]), 1)
        self.assertEqual(summary["downloaded"][0]["kind"], "video")
        # Video bytes never go back inline.
        self.assertEqual([r for r in result if isinstance(r, Image)], [])
        saved = Path(summary["downloaded"][0]["saved_path"])
        self.assertEqual(saved.name, "post_DYdWoL8gASP_00.mp4")

    def test_indices_filter_keeps_original_index_in_filename(self) -> None:
        carousel = (_image_item(), _image_item(), _image_item())
        result = self._call(
            carousel, outcomes=[_ok("image/jpeg", TINY_JPEG)], indices=[2]
        )
        summary = result[-1]
        self.assertEqual(len(summary["downloaded"]), 1)
        self.assertEqual(
            Path(summary["downloaded"][0]["saved_path"]).name,
            "post_DYdWoL8gASP_02.jpg",
        )

    def test_download_failure_is_skipped_with_reason(self) -> None:
        result = self._call((_image_item(),), outcomes=[_fail("http_404")])
        summary = result[-1]
        self.assertEqual(len(summary["downloaded"]), 0)
        self.assertEqual(summary["skipped"][0]["reason"], "http_404")


class ResolvePostMediaTests(unittest.TestCase):
    def _post(self, shortcode: str, media: tuple[MediaItem, ...]) -> Post:
        return Post(
            pk="1",
            id="1_2",
            shortcode=shortcode,
            url=f"https://www.instagram.com/p/{shortcode}/",
            media_type=1,
            caption="c",
            media=media,
            like_count=1,
            comment_count=0,
            view_count=None,
            taken_at=1700000000,
            product_type="feed",
            location=None,
            author=User("2", "carol", "Carol", False, False, None),
        )

    def test_returns_shortcode_and_media(self) -> None:
        post = self._post("DYdWoL8gASP", (_image_item(),))
        fetch = mock.AsyncMock(return_value=post)
        with mock.patch.object(media_mod.post, "fetch", new=fetch):
            shortcode, media = _run(
                media_mod.resolve_post_media(
                    object(), id_or_url="https://www.instagram.com/p/DYdWoL8gASP/"
                )
            )
        self.assertEqual(shortcode, "DYdWoL8gASP")
        self.assertEqual(len(media), 1)
        # The shortcode is extracted from the URL before fetching.
        self.assertEqual(fetch.call_args.kwargs["id_or_url"], "DYdWoL8gASP")

    def test_raises_when_post_missing(self) -> None:
        fetch = mock.AsyncMock(return_value=None)
        with mock.patch.object(media_mod.post, "fetch", new=fetch):
            with self.assertRaises(ValueError):
                _run(media_mod.resolve_post_media(object(), id_or_url="ZZZ"))


class SelectIndicesTests(unittest.TestCase):
    def test_none_returns_all_enumerated(self) -> None:
        items = (_image_item(), _video_item())
        self.assertEqual(
            [i for i, _ in server_mod._select_indices(items, None)], [0, 1]
        )

    def test_filters_to_requested_positions(self) -> None:
        items = (_image_item(), _video_item(), _image_item())
        self.assertEqual(
            [i for i, _ in server_mod._select_indices(items, [0, 2])], [0, 2]
        )


class ToolRegistrationTests(unittest.TestCase):
    def test_download_media_is_registered(self) -> None:
        tools = _run(server_mod.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertIn("download_media", names)
        self.assertEqual(len(names), 9)


if __name__ == "__main__":
    unittest.main()
