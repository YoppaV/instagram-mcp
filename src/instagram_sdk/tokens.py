"""Page tokens GraphQL needs, extracted from instagram.com's server HTML.

Pure. The HTML comes from ``page.content()`` of a document loaded in the
fixed tab. Tokens live in memory only: never written to disk or to logs, and
``Tokens.__repr__`` masks them.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .errors import TokenError

DEFAULT_APP_ID = "936619743392459"

_FB_DTSG = (
    re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"'),
    re.compile(r'"DTSGInitData",\[\],\{"token":"([^"]+)"'),
)
_LSD = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_ACTOR_ID = re.compile(r'"actorID":"(\d+)"')
_CSRF = re.compile(r'"csrf_token":"([^"]+)"')
_REVISION = re.compile(r'"client_revision":(\d+)')
_APP_ID = re.compile(r'"X-IG-App-ID":"(\d+)"')


def _mask(value: str | None) -> str:
    if not value:
        return "None"
    return f"<{len(value)} chars>"


@dataclass(frozen=True)
class Tokens:
    fb_dtsg: str
    lsd: str
    actor_id: str
    csrf_html: str | None = None
    client_revision: str | None = None
    app_id: str = DEFAULT_APP_ID
    obtained_at: float = field(default_factory=time.monotonic)

    @property
    def jazoest(self) -> str:
        return "2" + str(sum(ord(c) for c in self.fb_dtsg))

    def age_s(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.obtained_at

    def __repr__(self) -> str:
        return (
            f"Tokens(fb_dtsg={_mask(self.fb_dtsg)}, lsd={_mask(self.lsd)}, "
            f"actor_id={_mask(self.actor_id)}, client_revision={self.client_revision})"
        )

    __str__ = __repr__


def _first(patterns: tuple[re.Pattern[str], ...] | re.Pattern[str], html: str) -> str | None:
    if isinstance(patterns, re.Pattern):
        patterns = (patterns,)
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def extract_tokens(html: str) -> Tokens:
    """Tokens from a page's HTML. ``TokenError`` if any required one is missing."""
    html = html or ""
    fb_dtsg = _first(_FB_DTSG, html)
    lsd = _first(_LSD, html)
    actor_id = _first(_ACTOR_ID, html)
    missing = [
        name
        for name, value in (("fb_dtsg", fb_dtsg), ("lsd", lsd), ("actorID", actor_id))
        if not value
    ]
    if missing:
        raise TokenError("tokens", f"not found in page HTML: {', '.join(missing)}")
    return Tokens(
        fb_dtsg=fb_dtsg or "",
        lsd=lsd or "",
        actor_id=actor_id or "",
        csrf_html=_first(_CSRF, html),
        client_revision=_first(_REVISION, html),
        app_id=_first(_APP_ID, html) or DEFAULT_APP_ID,
    )


def try_extract_tokens(html: str) -> Tokens | None:
    try:
        return extract_tokens(html)
    except TokenError:
        return None
