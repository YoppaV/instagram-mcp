"""Session/auth helpers — load Playwright storage_state, detect expiry.

Instagram authentication is cookie-based. The ``storage_state`` JSON written by
``scripts/auth_login.py`` carries the logged-in cookies; the MCP server loads
it into a browser context on every tool call. Two cookies only exist while a
session is valid — ``sessionid`` and ``ds_user_id`` — so their presence is our
best-effort "authenticated" signal.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

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
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE_ID = "Etc/UTC"

_USER_AGENT_PLATFORMS = {
    "Windows": "Windows NT 10.0; Win64; x64",
    "Darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
}

_SESSION_SUFFIX = "_instagram_state"
_HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,30}")


def browser_locale() -> str:
    """Locale reported to instagram.com (``BROWSER_LOCALE``).

    Read at call time, not import time, so a value that comes from ``.env`` is
    honoured regardless of when ``load_dotenv`` ran. Align it with the
    account's real region: a fingerprint that matches the account history is
    harder to flag.
    """
    return os.getenv("BROWSER_LOCALE", "").strip() or DEFAULT_LOCALE


def browser_timezone_id() -> str:
    """Timezone reported to instagram.com (``BROWSER_TIMEZONE_ID``)."""
    return os.getenv("BROWSER_TIMEZONE_ID", "").strip() or DEFAULT_TIMEZONE_ID


def build_user_agent(browser_version: str, system: str | None = None) -> str:
    """User agent for the Chromium actually running on this OS.

    Headless Chromium advertises itself as ``HeadlessChrome``, so the default
    can't be kept; the override is derived from the real browser version and
    host OS so it stays truthful everywhere.
    """
    major = browser_version.split(".", 1)[0]
    token = _USER_AGENT_PLATFORMS.get(
        system or platform.system(), _USER_AGENT_PLATFORMS["Linux"]
    )
    return (
        f"Mozilla/5.0 ({token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def browser_context_options(browser_version: str) -> dict[str, object]:
    """Keyword arguments for ``browser.new_context``, shared by login and tools."""
    return {
        "viewport": VIEWPORT,
        "user_agent": build_user_agent(browser_version),
        "locale": browser_locale(),
        "timezone_id": browser_timezone_id(),
    }


def validate_handle(raw: str) -> str:
    """Return a clean Instagram handle, or raise ``ValueError``.

    The handle names the session file and the ``/<handle>/saved/`` URL, so an
    email or a malformed value would only surface later as an empty result.
    """
    handle = raw.strip().lstrip("@")
    if "@" in handle:
        raise ValueError(
            f"{raw!r} looks like an email. Use your Instagram username "
            "(the name in your profile URL), not your login email."
        )
    if not _HANDLE_RE.fullmatch(handle):
        raise ValueError(
            f"{raw!r} is not a valid Instagram username "
            "(1-30 letters, digits, dots or underscores)."
        )
    return handle


class SessionExpiredError(RuntimeError):
    """Raised when storage_state no longer authenticates against instagram.com."""


def is_login_redirect(url: str) -> bool:
    return any(frag in url for frag in LOGIN_URL_FRAGMENTS)


def handle_from_session_path(path: Path) -> str:
    """Extract the handle from ``sessions/<handle>_instagram_state.json``."""
    name = path.stem  # e.g. ``alice_instagram_state``
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


# Same app id the instagram.com web client sends with its own REST calls.
_WEB_APP_ID = "936619743392459"
_VIEWER_LOOKUP_JS = """
async ([userId, appId]) => {
  const response = await fetch(`/api/v1/users/${userId}/info/`, {
    headers: {"X-IG-App-ID": appId},
    credentials: "include",
  });
  if (!response.ok) return null;
  const body = await response.json();
  return body && body.user ? body.user.username : null;
}
"""


def fetch_logged_in_username(page: "Page", user_id: str) -> str | None:
    """Ask the logged-in page who it is. ``None`` if that can't be determined.

    Only used at login, once, to catch a ``--username`` that is not the
    account's. Best-effort: a failed lookup must never lose a finished login.
    """
    try:
        username = page.evaluate(_VIEWER_LOOKUP_JS, [user_id, _WEB_APP_ID])
    except Exception:
        return None
    return username if isinstance(username, str) and username else None


def resolve_session_handle(requested: str, real_username: str | None) -> str:
    """The handle to save under: the account's real one whenever it is known."""
    if real_username and real_username.lower() != requested.lower():
        return real_username
    return requested


def save_storage_state(context: "BrowserContext", path: Path) -> None:
    """Persist Playwright storage_state (cookies + localStorage) to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
