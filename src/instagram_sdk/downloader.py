"""Pure HTTP download helper — streams media bytes off Instagram's CDNs.

No Playwright here. The endpoints layer (``endpoints/media.py``) resolves which
media *URLs* belong to a post; this module turns those URLs into bytes.
``httpx`` is streamed so an oversized response can be aborted mid-flight without
buffering the whole thing.

Framework-free apart from the ``httpx.AsyncClient`` passed in by the caller —
which keeps it trivially unit-testable with a fake client. Mirrors the sibling
``twitter-mcp`` downloader; the only Instagram-specific bit is the host filter
(see ``is_downloadable_media_url``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    import httpx

# Safety net for images. Instagram images are small (a few MB at most), so this
# rarely trips — it just guards against a pathological response. Videos opted in
# via ``download_videos`` pass ``max_bytes=None`` (no cap by design).
IMAGE_MAX_BYTES = 25 * 1024 * 1024

# Instagram media CDN domains. Unlike Twitter's fixed hosts, Instagram serves
# media from rotating per-datacentre subdomains (``scontent-mad1-1...``,
# ``instagram.fxxx...``), so we match by domain *suffix*, not exact host. This
# still keeps downloads off any non-CDN URL.
_ALLOWED_HOST_SUFFIXES = ("cdninstagram.com", "fbcdn.net")

# ``reason`` values surfaced on a failed FetchOutcome / a SkippedMedia entry.
REASON_TOO_LARGE = "too_large"
REASON_HTTP_404 = "http_404"
REASON_DOWNLOAD_ERROR = "download_error"

_CONTENT_TYPE_FORMATS: dict[str, str] = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "video/mp4": "mp4",
}


@dataclass(frozen=True)
class FetchOutcome:
    """Result of a single ``fetch_bytes`` call.

    ``ok`` True ⇒ ``data`` holds the bytes. ``ok`` False ⇒ ``reason`` explains
    why (``http_404`` | ``too_large`` | ``download_error``).
    """

    ok: bool
    data: bytes | None
    content_type: str
    reason: str | None


async def fetch_bytes(
    client: "httpx.AsyncClient",
    url: str,
    *,
    max_bytes: int | None,
) -> FetchOutcome:
    """Stream ``url`` into memory, aborting if it exceeds ``max_bytes``.

    ``max_bytes=None`` disables the cap (used for opted-in video downloads).
    A 404 is distinguished from other failures so callers can report a
    deleted/expired asset distinctly from a transient network error.
    """
    try:
        async with client.stream("GET", url) as response:
            status = response.status_code
            if status == 404:
                return FetchOutcome(False, None, "", REASON_HTTP_404)
            if status >= 400:
                return FetchOutcome(False, None, "", REASON_DOWNLOAD_ERROR)

            content_type = response.headers.get("content-type", "") or ""
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    return FetchOutcome(False, None, content_type, REASON_TOO_LARGE)
                chunks.append(chunk)
            return FetchOutcome(True, b"".join(chunks), content_type, None)
    except Exception:
        # Connection reset, DNS failure, timeout, malformed response — all
        # collapse to a single best-effort error; the caller keeps going.
        return FetchOutcome(False, None, "", REASON_DOWNLOAD_ERROR)


def content_type_to_format(content_type: str, fallback_ext: str) -> str:
    """Map a Content-Type header (or a fallback file extension) to a format.

    Returns one of ``jpeg`` / ``png`` / ``webp`` / ``gif`` / ``heic`` / ``mp4``.
    The format string feeds ``mcp.server.fastmcp.Image(format=...)``; use
    ``format_to_extension`` to turn it into a filename suffix.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CONTENT_TYPE_FORMATS:
        return _CONTENT_TYPE_FORMATS[ct]

    ext = (fallback_ext or "").lower().lstrip(".")
    if ext in ("jpg", "jpeg"):
        return "jpeg"
    if ext in ("png", "webp", "gif", "heic", "mp4"):
        return ext
    return "jpeg"


def format_to_extension(fmt: str) -> str:
    """Turn a format string into a filename extension (``jpeg`` → ``jpg``)."""
    return "jpg" if fmt == "jpeg" else fmt


def extension_from_url(url: str) -> str:
    """Best-effort file extension from a media URL's path (query string ignored).

    Instagram URLs look like ``.../702259465_127...n.jpg?stp=...&_nc_cat=102``;
    the path suffix is a reliable fallback when the Content-Type is missing.
    """
    if not url:
        return ""
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    if "." not in path:
        return ""
    suffix = path.rsplit(".", 1)[1].lower()
    return suffix if 0 < len(suffix) <= 5 else ""


def is_downloadable_media_url(url: str) -> bool:
    """True only for ``http(s)`` URLs on a known Instagram media CDN domain.

    Rejects ``blob:`` / ``data:`` URIs and any non-CDN host — a safety filter so
    the download tool never fetches an unrelated URL.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _ALLOWED_HOST_SUFFIXES
    )
