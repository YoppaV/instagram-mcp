"""Session/auth helpers — load Playwright storage_state, detect expiry.

Instagram authentication is cookie-based. The ``storage_state`` JSON written by
``scripts/auth_login.py`` carries the logged-in cookies; the MCP server loads
it into a browser context on every tool call. A live (present, unexpired)
``sessionid`` is our best-effort "authenticated" signal. ``ds_user_id`` is
set at login too but can expire months before the session does (seen: expired
in August on a session that still works in November), so it is not required;
the account's own id comes from the page's ``actorID`` instead.
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
# Cookies that must be live for a session to count as authenticated.
REQUIRED_COOKIES: tuple[str, ...] = ("sessionid",)
# Cookies the interactive login waits for (both appear right after login).
LOGIN_COOKIES: tuple[str, ...] = ("sessionid", "ds_user_id")
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


def browser_context_options(
    browser_version: str,
    *,
    locale: str | None = None,
    timezone_id: str | None = None,
    viewport: dict[str, int] | None = None,
) -> dict[str, object]:
    """Keyword arguments for ``browser.new_context``, shared by login and tools."""
    return {
        "viewport": dict(viewport or VIEWPORT),
        "user_agent": build_user_agent(browser_version),
        "locale": locale or browser_locale(),
        "timezone_id": timezone_id or browser_timezone_id(),
    }


# --------------------------------------------------------------------------
# Client Hints: make sec-ch-ua* and navigator.userAgentData agree with the UA.
# --------------------------------------------------------------------------
# Playwright's ``user_agent`` option rewrites the UA string only; the
# ``sec-ch-ua`` headers kept saying "HeadlessChrome". The fix is a CDP
# ``Emulation.setUserAgentOverride`` with full metadata, declaring Google
# Chrome (what the UA string already claims) on the host's real OS.
_GREASE_CHARS = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
_GREASE_VERSIONS = ["8", "99", "24"]
_BRAND_ORDERS = [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]]
CLIENT_HINTS_BRAND = "Google Chrome"

_NAV_PLATFORMS = {"Windows": "Win32", "Darwin": "MacIntel", "Linux": "Linux x86_64"}
_CH_PLATFORMS = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}


def greased_brands(
    major: int, brand: str = CLIENT_HINTS_BRAND, full_version: str | None = None
) -> list[dict[str, str]]:
    """Chromium's brand list for ``major`` (same GREASE and order as the binary).

    With ``full_version`` the versions are the full ones
    (``sec-ch-ua-full-version-list``); the GREASE brand gets ``<v>.0.0.0``.
    """
    grease_v = _GREASE_VERSIONS[major % 3]
    grease = {
        "brand": f"Not{_GREASE_CHARS[major % 11]}A{_GREASE_CHARS[(major + 1) % 11]}Brand",
        "version": f"{grease_v}.0.0.0" if full_version else grease_v,
    }
    version = full_version or str(major)
    chromium = {"brand": "Chromium", "version": version}
    product = {"brand": brand, "version": version}
    order = _BRAND_ORDERS[major % 6]
    out: list[dict[str, str]] = [{}, {}, {}]
    out[order[0]], out[order[1]], out[order[2]] = grease, chromium, product
    return out


def _platform_version(system: str, release: str, mac_version: str = "") -> str:
    if system == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
        except (ValueError, IndexError):
            build = 0
        return "15.0.0" if build >= 22000 else "10.0.0"
    if system == "Darwin":
        parts = (mac_version or "10.15.7").split(".")
        return ".".join((parts + ["0", "0"])[:3])
    match = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", release or "")
    if not match:
        return "0.0.0"
    return ".".join(g or "0" for g in match.groups())


def client_hints_metadata(
    browser_version: str,
    system: str | None = None,
    *,
    release: str | None = None,
    machine: str | None = None,
    mac_version: str | None = None,
) -> dict[str, object]:
    """``userAgentMetadata`` for ``Emulation.setUserAgentOverride``. Pure."""
    system = system or platform.system()
    if system not in _CH_PLATFORMS:
        system = "Linux"
    major = int(browser_version.split(".", 1)[0])
    machine = (machine if machine is not None else platform.machine()).lower()
    arm = machine.startswith(("arm", "aarch"))
    return {
        "brands": greased_brands(major),
        "fullVersionList": greased_brands(major, full_version=browser_version),
        "fullVersion": browser_version,
        "platform": _CH_PLATFORMS[system],
        "platformVersion": _platform_version(
            system,
            release if release is not None else platform.release(),
            mac_version if mac_version is not None else platform.mac_ver()[0],
        ),
        "architecture": "arm" if arm else "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "wow64": False,
    }


def user_agent_override(
    browser_version: str, locale: str | None = None, system: str | None = None
) -> dict[str, object]:
    """Parameters for CDP ``Emulation.setUserAgentOverride``.

    Send it on a page's CDP session before its first navigation; it aligns
    the UA string, ``sec-ch-ua*`` and ``navigator.userAgentData`` at once.
    """
    system = system or platform.system()
    return {
        "userAgent": build_user_agent(browser_version, system),
        "acceptLanguage": locale or browser_locale(),
        "platform": _NAV_PLATFORMS.get(system, _NAV_PLATFORMS["Linux"]),
        "userAgentMetadata": client_hints_metadata(browser_version, system),
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


# Moved to ``errors``; re-exported so ``from instagram_sdk.auth import
# SessionExpiredError`` keeps working for library users.
from .errors import SessionExpiredError  # noqa: E402,F401


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

    cookies = [c for c in (raw.get("cookies") or []) if isinstance(c, dict)]
    now = time.time()

    def live(cookie: dict) -> bool:
        expires = cookie.get("expires")
        # -1 / missing = session cookie (no expiry).
        return not isinstance(expires, (int, float)) or expires <= 0 or expires > now

    live_names = {c.get("name") for c in cookies if live(c)}
    expired = sorted({c.get("name") for c in cookies if not live(c)} - live_names)
    missing = [name for name in REQUIRED_COOKIES if name not in live_names]
    summary["missing_cookies"] = missing
    summary["expired_cookies"] = [n for n in expired if n in LOGIN_COOKIES]
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
    """Poll cookies until all LOGIN_COOKIES are present or timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cookies = collect_instagram_cookies(context)
        if all(name in cookies for name in LOGIN_COOKIES):
            return cookies
        time.sleep(LOGIN_POLL_INTERVAL_S)

    cookies = collect_instagram_cookies(context)
    missing = [name for name in LOGIN_COOKIES if name not in cookies]
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
