"""Session/auth helpers — load Playwright storage_state, detect expiry.

Instagram authentication is cookie-based. The ``storage_state`` JSON written by
``scripts/auth_login.py`` carries the logged-in cookies; the MCP server loads
it into a browser context on every tool call. Two cookies only exist while a
session is valid — ``sessionid`` and ``ds_user_id`` — so their presence is our
best-effort "authenticated" signal.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

BASE = "https://www.instagram.com"
INSTAGRAM_ORIGIN = "https://www.instagram.com"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"
# Instagram bounces unauthenticated requests to /accounts/login.
LOGIN_URL_FRAGMENTS: tuple[str, ...] = ("/accounts/login",)
# Cookies that exist only once a real account session is established.
REQUIRED_COOKIES: tuple[str, ...] = ("sessionid", "ds_user_id")
DEFAULT_LOGIN_TIMEOUT_S = 600
LOGIN_POLL_INTERVAL_S = 1.5

VIEWPORT = {"width": 1280, "height": 900}
LOCALE = "es-ES"
TIMEZONE_ID = "Europe/Madrid"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SESSION_SUFFIX = "_instagram_state"


class SessionExpiredError(RuntimeError):
    """Raised when storage_state no longer authenticates against instagram.com."""


def is_login_redirect(url: str) -> bool:
    return any(frag in url for frag in LOGIN_URL_FRAGMENTS)


def handle_from_session_path(path: Path) -> str:
    """Extract the handle from ``sessions/<handle>_instagram_state.json``."""
    name = path.stem  # ``jrdiaz_instagram_state``
    if name.endswith(_SESSION_SUFFIX):
        return name[: -len(_SESSION_SUFFIX)]
    return name


def session_summary(session_path: Path) -> dict[str, object]:
    """Return ``{authenticated, handle, session_path, last_login, missing_cookies}``.

    ``authenticated`` is best-effort: True iff the file exists and carries the
    required cookies. Whether the cookies still WORK against instagram.com
    needs an actual request — any other tool surfaces that via
    ``SessionExpiredError``.
    """
    summary: dict[str, object] = {
        "authenticated": False,
        "handle": handle_from_session_path(session_path),
        "session_path": str(session_path),
        "last_login": None,
        "missing_cookies": list(REQUIRED_COOKIES),
    }
    if not session_path.exists():
        return summary

    try:
        raw = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return summary

    cookies = raw.get("cookies") or []
    cookie_names = {c.get("name") for c in cookies}
    missing = [name for name in REQUIRED_COOKIES if name not in cookie_names]
    summary["missing_cookies"] = missing
    summary["authenticated"] = not missing

    try:
        mtime = datetime.fromtimestamp(session_path.stat().st_mtime, tz=timezone.utc)
        summary["last_login"] = mtime.isoformat()
    except OSError:
        pass

    return summary


def collect_instagram_cookies(context: "BrowserContext") -> dict[str, str]:
    """Return a flat ``{name: value}`` dict for all instagram.com cookies."""
    cookies = context.cookies(INSTAGRAM_ORIGIN)
    return {c["name"]: c["value"] for c in cookies}


def wait_for_instagram_login(
    context: "BrowserContext",
    timeout_s: int = DEFAULT_LOGIN_TIMEOUT_S,
) -> dict[str, str]:
    """Poll cookies until all REQUIRED_COOKIES are present or timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cookies = collect_instagram_cookies(context)
        if all(name in cookies for name in REQUIRED_COOKIES):
            return cookies
        time.sleep(LOGIN_POLL_INTERVAL_S)

    cookies = collect_instagram_cookies(context)
    missing = [name for name in REQUIRED_COOKIES if name not in cookies]
    raise TimeoutError(
        f"Timed out after {timeout_s}s waiting for Instagram login. "
        f"Missing cookies: {missing}"
    )


def save_storage_state(context: "BrowserContext", path: Path) -> None:
    """Persist Playwright storage_state (cookies + localStorage) to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
