"""Single place where the server reads its environment.

``Settings.from_env()`` is called when the runtime is built (and on each
session lookup), never at import time, so values from ``.env`` are honoured
whenever ``load_dotenv`` ran. No path assumes the repo or ``Path.home()``
beyond the defaults: in a container every path is an env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IDLE_TIMEOUT_S = 180
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE_ID = "Etc/UTC"
DEFAULT_MAX_PER_HOUR = 150
DEFAULT_MAX_PER_DAY = 800

_HOME_ROOT = Path.home() / ".instagram-mcp"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LEGACY_SESSION_DIR = _PROJECT_ROOT / "sessions"
SESSION_GLOB = "*_instagram_state.json"


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_path(name: str) -> Path | None:
    raw = _env(name)
    return Path(raw).expanduser() if raw else None


@dataclass(frozen=True)
class Settings:
    session_file: Path | None
    session_dir: Path | None
    username: str
    state_dir: Path
    downloads_dir: Path
    headless: bool
    idle_timeout_s: int
    channel: str | None
    block_js: bool
    locale: str
    timezone_id: str
    default_account: str = ""
    max_requests_per_hour: int = DEFAULT_MAX_PER_HOUR
    max_requests_per_day: int = DEFAULT_MAX_PER_DAY
    allow_degraded_state: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            session_file=_env_path("INSTAGRAM_SESSION_FILE"),
            session_dir=_env_path("INSTAGRAM_SESSION_DIR"),
            username=_env("INSTAGRAM_USERNAME"),
            state_dir=_env_path("INSTAGRAM_STATE_DIR") or _HOME_ROOT / "state",
            downloads_dir=_env_path("INSTAGRAM_DOWNLOADS_DIR") or _HOME_ROOT / "downloads",
            headless=_env_bool("BROWSER_HEADLESS", True),
            idle_timeout_s=_env_int("BROWSER_IDLE_TIMEOUT_S", DEFAULT_IDLE_TIMEOUT_S),
            channel=_env("BROWSER_CHANNEL") or None,
            block_js=_env_bool("INSTAGRAM_BLOCK_JS", True),
            locale=_env("BROWSER_LOCALE") or DEFAULT_LOCALE,
            timezone_id=_env("BROWSER_TIMEZONE_ID") or DEFAULT_TIMEZONE_ID,
            default_account=_env("INSTAGRAM_DEFAULT_ACCOUNT").lstrip("@"),
            max_requests_per_hour=_env_int("INSTAGRAM_MAX_REQUESTS_PER_HOUR", DEFAULT_MAX_PER_HOUR),
            max_requests_per_day=_env_int("INSTAGRAM_MAX_REQUESTS_PER_DAY", DEFAULT_MAX_PER_DAY),
            allow_degraded_state=_env_bool("INSTAGRAM_ALLOW_DEGRADED_STATE", False),
        )

    def candidate_session_dirs(self) -> list[Path]:
        """Where ``<handle>_instagram_state.json`` files are looked up, in order.

        ``INSTAGRAM_SESSION_DIR`` replaces the default; otherwise
        ``~/.instagram-mcp/sessions/`` and then the legacy in-repo
        ``sessions/`` folder.
        """
        if self.session_dir is not None:
            return [self.session_dir]
        return [_HOME_ROOT / "sessions", _LEGACY_SESSION_DIR]
