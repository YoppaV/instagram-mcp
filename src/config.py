"""Configuration loader. Reads ``.env`` and exposes paths + Instagram handle."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = PROJECT_ROOT / "sessions"


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

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    return Config(
        instagram_username=instagram_username,
        session_dir=SESSION_DIR,
    )


cfg = load_config()
