"""Interactive Instagram login through a Playwright-controlled browser.

Launches a headed Chromium window, navigates to the Instagram login page, and
waits for the user to authenticate manually (password, 2FA, "save your login
info", suspicious-login checkpoints — whatever Instagram throws up). As soon as
the session cookies (``sessionid`` + ``ds_user_id``) appear, the full
``storage_state`` is serialized to ``sessions/<handle>_instagram_state.json``.
The MCP server reads this file on every tool call.

Run from the project root::

    venv/bin/python -m scripts.auth_login --username yourhandle

Designed for WSL2 + WSLg: the browser window is painted on Windows directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

from instagram_sdk.auth import (  # noqa: E402
    DEFAULT_LOGIN_TIMEOUT_S,
    LOCALE,
    LOGIN_URL,
    TIMEZONE_ID,
    USER_AGENT,
    VIEWPORT,
    save_storage_state,
    wait_for_instagram_login,
)

from src.config import ConfigError, cfg  # noqa: E402

console = Console()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive Instagram login via Playwright (WSLg-friendly)."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless. Defeats the purpose of interactive login; escape hatch only.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_S,
        help=f"Seconds to wait for the user to finish login (default: {DEFAULT_LOGIN_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--username",
        type=str,
        default=None,
        help="Instagram handle (defaults to INSTAGRAM_USERNAME from .env).",
    )
    return parser.parse_args()


def _launch_and_save(headless: bool, timeout_s: int, session_file: Path) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=headless)
        except PlaywrightError as exc:
            raise RuntimeError(
                f"Failed to launch Chromium: {exc}\n"
                "Install browser binaries first: python -m playwright install chromium\n"
                "If native libs are missing on WSL2: "
                "sudo python -m playwright install-deps chromium"
            ) from exc

        try:
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
            )
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            console.print(
                f"[yellow]Log in to instagram.com in the browser window. "
                f"Waiting up to {timeout_s}s for sessionid + ds_user_id "
                f"cookies...[/yellow]"
            )
            wait_for_instagram_login(context, timeout_s)
            save_storage_state(context, session_file)
        finally:
            browser.close()


def main() -> int:
    args = _parse_args()

    handle = (args.username or cfg.instagram_username).strip()
    if not handle:
        console.print(
            "[red]INSTAGRAM_USERNAME not set in .env and --username not provided.[/red]"
        )
        return 2

    session_file = cfg.session_dir / f"{handle}_instagram_state.json"

    try:
        _launch_and_save(args.headless, args.timeout, session_file)
    except (RuntimeError, TimeoutError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    console.print(f"[green]Wrote storage state for {handle} to {session_file}[/green]")
    console.print(
        "[green]Now start the MCP server: venv/bin/python -m instagram_sdk.server[/green]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
