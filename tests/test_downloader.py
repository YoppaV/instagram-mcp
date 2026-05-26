"""Tests for the media-download layer: dataclasses + the ``downloader`` helper.

The ``downloader`` is pure (no Playwright): it streams bytes off Instagram's CDN
via an ``httpx.AsyncClient``. Here we drive it with a fake client whose
``stream()`` returns an async context manager that yields canned chunks, so
nothing touches the network.

Run with: ``venv/bin/python -m unittest tests.test_downloader -v``
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import downloader  # noqa: E402
from instagram_sdk.models import (  # noqa: E402
    DownloadedMedia,
    MediaDownloadResult,
    SkippedMedia,
)

FIXTURES = Path(__file__).parent / "fixtures"
TINY_JPEG = (FIXTURES / "tiny.jpg").read_bytes()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeStreamResponse:
    """Async context manager mimicking ``httpx``'s streaming Response."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._raise_exc = raise_exc

    async def __aenter__(self) -> "FakeStreamResponse":
        if self._raise_exc is not None:
            raise self._raise_exc
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def aiter_bytes(self) -> Any:
        for chunk in self._chunks:
            yield chunk


class FakeAsyncClient:
    """Mimics the surface of ``httpx.AsyncClient`` used by ``fetch_bytes``."""

    def __init__(self, response: FakeStreamResponse) -> None:
        self._response = response
        self.stream_calls: list[tuple[str, str]] = []

    def stream(self, method: str, url: str, **_: Any) -> FakeStreamResponse:
        self.stream_calls.append((method, url))
        return self._response


_IMG = "https://scontent-mad1-1.cdninstagram.com/v/t51.x/photo.jpg?_nc_ohc=x"
_VID = "https://scontent-mad2-1.cdninstagram.com/o1/v/t2/f2/video.mp4?efg=y"


class DataclassTests(unittest.TestCase):
    def test_downloaded_media_roundtrips(self) -> None:
        dm = DownloadedMedia(
            kind="image",
            source_url=_IMG,
            saved_path="/tmp/post_AAA1_00.jpg",
            byte_size=287,
            content_type="image/jpeg",
        )
        d = asdict(dm)
        self.assertEqual(d["kind"], "image")
        self.assertEqual(d["byte_size"], 287)
        self.assertEqual(d["content_type"], "image/jpeg")

    def test_skipped_media_holds_reason(self) -> None:
        sm = SkippedMedia(
            kind="video", source_url=_VID, reason="video_not_requested"
        )
        self.assertEqual(sm.kind, "video")
        self.assertEqual(sm.reason, "video_not_requested")

    def test_media_download_result_holds_both_lists(self) -> None:
        result = MediaDownloadResult(
            shortcode="DYdWoL8gASP", downloaded=(), skipped=()
        )
        self.assertEqual(result.shortcode, "DYdWoL8gASP")
        self.assertEqual(asdict(result)["downloaded"], ())


class FetchBytesTests(unittest.TestCase):
    def test_success_concatenates_chunks(self) -> None:
        response = FakeStreamResponse(
            status_code=200,
            headers={"content-type": "image/jpeg"},
            chunks=[TINY_JPEG[:120], TINY_JPEG[120:]],
        )
        client = FakeAsyncClient(response)
        outcome = _run(
            downloader.fetch_bytes(
                client, _IMG, max_bytes=downloader.IMAGE_MAX_BYTES
            )
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.data, TINY_JPEG)
        self.assertEqual(outcome.content_type, "image/jpeg")
        self.assertIsNone(outcome.reason)
        self.assertEqual(client.stream_calls, [("GET", _IMG)])

    def test_404_is_reported(self) -> None:
        outcome = _run(
            downloader.fetch_bytes(
                FakeAsyncClient(FakeStreamResponse(status_code=404)),
                _IMG,
                max_bytes=None,
            )
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "http_404")
        self.assertIsNone(outcome.data)

    def test_server_error_is_download_error(self) -> None:
        outcome = _run(
            downloader.fetch_bytes(
                FakeAsyncClient(FakeStreamResponse(status_code=503)),
                _IMG,
                max_bytes=None,
            )
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "download_error")

    def test_oversized_aborts(self) -> None:
        response = FakeStreamResponse(
            status_code=200,
            headers={"content-type": "image/jpeg"},
            chunks=[b"a" * 100, b"b" * 100, b"c" * 100],
        )
        outcome = _run(
            downloader.fetch_bytes(
                FakeAsyncClient(response), _IMG, max_bytes=150
            )
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "too_large")

    def test_network_error_is_download_error(self) -> None:
        response = FakeStreamResponse(raise_exc=RuntimeError("connection reset"))
        outcome = _run(
            downloader.fetch_bytes(
                FakeAsyncClient(response), _IMG, max_bytes=None
            )
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "download_error")

    def test_max_bytes_none_allows_large_payload(self) -> None:
        big = b"z" * (5 * 1024 * 1024)
        response = FakeStreamResponse(
            status_code=200,
            headers={"content-type": "video/mp4"},
            chunks=[big],
        )
        outcome = _run(
            downloader.fetch_bytes(
                FakeAsyncClient(response), _VID, max_bytes=None
            )
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.data or b""), len(big))


class IsDownloadableUrlTests(unittest.TestCase):
    def test_accepts_instagram_cdn_subdomains(self) -> None:
        self.assertTrue(downloader.is_downloadable_media_url(_IMG))
        self.assertTrue(downloader.is_downloadable_media_url(_VID))
        self.assertTrue(
            downloader.is_downloadable_media_url(
                "https://instagram.fmad1-1.fbcdn.net/v/t51/x.jpg"
            )
        )

    def test_rejects_blob_and_data_uris(self) -> None:
        self.assertFalse(
            downloader.is_downloadable_media_url("blob:https://instagram.com/abc")
        )
        self.assertFalse(
            downloader.is_downloadable_media_url("data:image/png;base64,iVBOR=")
        )

    def test_rejects_non_cdn_host(self) -> None:
        self.assertFalse(
            downloader.is_downloadable_media_url("https://evil.example.com/x.jpg")
        )

    def test_rejects_lookalike_host(self) -> None:
        # endswith check must not be fooled by a suffix that isn't a real
        # subdomain boundary.
        self.assertFalse(
            downloader.is_downloadable_media_url("https://evilcdninstagram.com/x")
        )

    def test_rejects_empty(self) -> None:
        self.assertFalse(downloader.is_downloadable_media_url(""))


class ContentTypeToFormatTests(unittest.TestCase):
    def test_known_content_types(self) -> None:
        self.assertEqual(downloader.content_type_to_format("image/jpeg", "jpg"), "jpeg")
        self.assertEqual(downloader.content_type_to_format("image/png", "jpg"), "png")
        self.assertEqual(downloader.content_type_to_format("image/webp", "jpg"), "webp")
        self.assertEqual(downloader.content_type_to_format("video/mp4", "mp4"), "mp4")

    def test_strips_parameters(self) -> None:
        self.assertEqual(
            downloader.content_type_to_format("image/webp; charset=binary", "jpg"),
            "webp",
        )

    def test_falls_back_to_extension(self) -> None:
        self.assertEqual(downloader.content_type_to_format("", "png"), "png")
        self.assertEqual(
            downloader.content_type_to_format("application/octet-stream", "mp4"),
            "mp4",
        )
        self.assertEqual(downloader.content_type_to_format("", "jpg"), "jpeg")

    def test_format_to_extension(self) -> None:
        self.assertEqual(downloader.format_to_extension("jpeg"), "jpg")
        self.assertEqual(downloader.format_to_extension("png"), "png")
        self.assertEqual(downloader.format_to_extension("mp4"), "mp4")


class ExtensionFromUrlTests(unittest.TestCase):
    def test_extracts_extension_ignoring_query(self) -> None:
        self.assertEqual(downloader.extension_from_url(_IMG), "jpg")
        self.assertEqual(downloader.extension_from_url(_VID), "mp4")

    def test_handles_no_extension(self) -> None:
        self.assertEqual(
            downloader.extension_from_url("https://x.cdninstagram.com/v/abc"), ""
        )

    def test_handles_empty(self) -> None:
        self.assertEqual(downloader.extension_from_url(""), "")


if __name__ == "__main__":
    unittest.main()
