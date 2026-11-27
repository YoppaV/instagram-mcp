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

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
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


# --------------------------------------------------------------------------
# Where downloads land
# --------------------------------------------------------------------------
_SAFE_PART = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_ACCOUNT = re.compile(r"^[a-z0-9._]{1,30}$")


def media_path(root: Path, account: str, shortcode: str, pk: str | None,
               index: int, ext: str) -> Path:
    """``<root>/<account>/post_<shortcode>_<pk>_<NN>.<ext>``.

    One folder per account (its handle, casefolded). The numeric ``pk``
    keeps posts apart on case-insensitive file systems (Windows, macOS, SMB),
    where two shortcodes that differ only in case would otherwise collide.
    The same media always maps to the same name, so re-downloading is
    idempotent. Every part is validated and the result must stay under
    ``root``.
    """
    folder = account.casefold()
    if not _SAFE_ACCOUNT.match(folder) or not folder.strip("."):
        raise ValueError(f"unsafe account folder name: {account!r}")
    if not _SAFE_PART.match(shortcode or ""):
        raise ValueError(f"unsafe shortcode: {shortcode!r}")
    ext = (ext or "").lower()
    if not re.match(r"^[a-z0-9]{1,5}$", ext):
        raise ValueError(f"unsafe extension: {ext!r}")
    pk_part = str(pk) if pk and str(pk).isdigit() else "0"
    name = f"post_{shortcode}_{pk_part}_{int(index):02d}.{ext}"
    root_resolved = root.expanduser().resolve()
    path = (root_resolved / folder / name).resolve()
    if root_resolved not in path.parents:
        raise ValueError(f"download path escapes the downloads directory: {path}")
    return path


def write_atomic(path: Path, data: bytes) -> None:
    """Write through a unique temporary file + rename: never half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Streaming to disk (bulk downloads)
# --------------------------------------------------------------------------
_STREAM_CHUNK = 256 * 1024


@dataclass(frozen=True)
class StreamOutcome:
    """Result of ``stream_to_file``: the file is on disk only when ``ok``."""

    ok: bool
    path: Path | None
    byte_size: int
    sha256: str | None
    content_type: str
    reason: str | None


def existing_media_file(directory: Path, stem: str) -> Path | None:
    """A finished file ``<stem>.<ext>`` already in ``directory`` (any extension)."""
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.glob(f"{stem}.*")):
        if candidate.suffix == ".part" or candidate.name.startswith("."):
            continue
        if candidate.is_file():
            return candidate
    return None


def sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_STREAM_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


async def stream_to_file(
    client: "httpx.AsyncClient",
    url: str,
    *,
    path_for_ext,
    max_bytes: int | None,
) -> StreamOutcome:
    """Stream ``url`` straight to disk; never holds the whole body in RAM.

    ``path_for_ext(ext)`` returns the final path once the Content-Type is
    known. Bytes go to a unique ``.part`` file in the same folder, hashed on
    the way, and are renamed into place only when complete (``os.replace``):
    a crash leaves at most a stray ``.part``, never half a media file.
    """
    import hashlib

    tmp: str | None = None
    try:
        async with client.stream("GET", url) as response:
            status = response.status_code
            if status == 404:
                return StreamOutcome(False, None, 0, None, "", REASON_HTTP_404)
            if status >= 400:
                return StreamOutcome(False, None, 0, None, "", REASON_DOWNLOAD_ERROR)
            content_type = response.headers.get("content-type", "") or ""
            length = response.headers.get("content-length")
            if max_bytes is not None and length and length.isdigit() and int(length) > max_bytes:
                return StreamOutcome(False, None, 0, None, content_type, REASON_TOO_LARGE)
            fmt = content_type_to_format(content_type, extension_from_url(url))
            final = path_for_ext(format_to_extension(fmt))
            final.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".part",
                                       dir=str(final.parent))
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as fh:
                async for chunk in response.aiter_bytes(_STREAM_CHUNK):
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise _TooLarge()
                    digest.update(chunk)
                    fh.write(chunk)
            os.replace(tmp, final)
            tmp = None
            return StreamOutcome(True, final, total, digest.hexdigest(), content_type, None)
    except _TooLarge:
        return StreamOutcome(False, None, 0, None, "", REASON_TOO_LARGE)
    except Exception:
        return StreamOutcome(False, None, 0, None, "", REASON_DOWNLOAD_ERROR)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _TooLarge(Exception):
    pass
