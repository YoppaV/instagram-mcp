"""Configuration loader. Reads ``.env`` and exposes paths + Instagram handle.

Used by ``scripts/auth_login.py`` to decide where to write a fresh
``storage_state.json``. The MCP server has its own resolver in
``instagram_sdk.server`` that also checks the legacy in-repo ``sessions/``
directory for backward compatibility — login always writes to the primary
location chosen here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_session_dir() -> Path:
    """Primary directory for Playwright storage_state files.

    Defaults to ``~/.instagram-mcp/sessions/`` so sessions never live inside
    the repo. Override with ``INSTAGRAM_SESSION_DIR``.
    """
    override = os.getenv("INSTAGRAM_SESSION_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".instagram-mcp" / "sessions"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    instagram_username: str
    session_dir: Path

    @property
    def instagram_session_file(self) -> Path:
        return self.session_dir / f"{self.instagram_username}_instagram_state.json"


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    instagram_username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    session_dir = _resolve_session_dir()
    session_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        instagram_username=instagram_username,
        session_dir=session_dir,
    )


cfg = load_config()
