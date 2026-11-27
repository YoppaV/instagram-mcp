r"""Interactive Instagram login through a Playwright-controlled browser.

Launches a headed Chromium window, navigates to the Instagram login page, and
waits for the user to authenticate manually (password, 2FA, "save your login
info", suspicious-login checkpoints — whatever Instagram throws up). As soon as
the session cookies (``sessionid`` + ``ds_user_id``) appear, the full
``storage_state`` is serialized to ``<session_dir>/<handle>_instagram_state.json``,
next to a sidecar ``<handle>_instagram_meta.json`` (user id, locale, timezone
and viewport used at login, no secrets) so the server reuses the same
fingerprint for that account. The MCP server reads these files on every call.

Each account is its own pair of files: logging in another account adds a
new pair and never touches the others. Re-running for the same account
replaces only its own files (written atomically, so an aborted login keeps
the previous session intact).

Run from the project root::

    venv/bin/python -m scripts.auth_login --username yourhandle      # macOS, Linux, WSL2
    venv\Scripts\python -m scripts.auth_login --username yourhandle   # Windows
    venv/bin/python -m scripts.auth_login --username second --locale es-ES \
        --timezone-id Europe/Madrid                                   # add another account

The browser window opens natively; on WSL2 it is painted through WSLg.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

from rich.console import Console

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

from instagram_sdk.accounts import SESSION_GLOB, build_meta, write_meta  # noqa: E402
from instagram_sdk.auth import (  # noqa: E402
    DEFAULT_LOGIN_TIMEOUT_S,
    LOGIN_URL,
    VIEWPORT,
    browser_context_options,
    browser_locale,
    browser_timezone_id,
    fetch_logged_in_username,
    resolve_session_handle,
    user_agent_override,
    validate_handle,
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
    parser.add_argument(
        "--locale",
        type=str,
        default=None,
        help="Browser locale for this account, e.g. es-ES (default: BROWSER_LOCALE or en-US). "
        "Saved in the sidecar; the server reuses it for this account.",
    )
    parser.add_argument(
        "--timezone-id",
        type=str,
        default=None,
        help="Browser timezone for this account, e.g. Europe/Madrid "
        "(default: BROWSER_TIMEZONE_ID or Etc/UTC). Saved in the sidecar.",
    )
    return parser.parse_args()


def _save_session_atomically(context, path: Path) -> None:
    """``storage_state`` into a temp file, then rename over ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    try:
        context.storage_state(path=str(tmp))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _launch_and_save(
    headless: bool,
    timeout_s: int,
    handle: str,
    session_dir: Path,
    locale: str | None = None,
    timezone_id: str | None = None,
) -> tuple[str, Path]:
    """Run the login and return the handle and file it was saved under."""
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
            hint = "Install browser binaries first: python -m playwright install chromium"
            if platform.system() == "Linux":
                hint += (
                    "\nIf native libs are missing on Linux/WSL2: "
                    "sudo python -m playwright install-deps chromium"
                )
            raise RuntimeError(f"Failed to launch Chromium: {exc}\n{hint}") from exc

        locale = locale or browser_locale()
        timezone_id = timezone_id or browser_timezone_id()
        try:
            context = browser.new_context(
                **browser_context_options(
                    browser.version, locale=locale, timezone_id=timezone_id, viewport=VIEWPORT
                )
            )
            page = context.new_page()
            # Same fingerprint as the server: sec-ch-ua* agree with the UA.
            try:
                cdp = context.new_cdp_session(page)
                cdp.send(
                    "Emulation.setUserAgentOverride",
                    user_agent_override(browser.version, locale),
                )
            except PlaywrightError:
                pass
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            console.print(
                f"[yellow]Log in to instagram.com in the browser window. "
                f"Waiting up to {timeout_s}s for sessionid + ds_user_id "
                f"cookies...[/yellow]"
            )
            cookies = wait_for_instagram_login(context, timeout_s)
            real_username = fetch_logged_in_username(page, cookies["ds_user_id"])
            final_handle = resolve_session_handle(handle, real_username)
            if real_username is None:
                console.print(
                    f"[yellow]Could not confirm that this account is {handle}. "
                    "If get_saved_posts returns nothing, re-run with your exact "
                    "username.[/yellow]"
                )
            elif final_handle != handle:
                console.print(
                    f"[yellow]--username was {handle}, but the account that "
                    f"logged in is {final_handle}. Using {final_handle}.[/yellow]"
                )
            session_file = session_dir / f"{final_handle}_instagram_state.json"
            _save_session_atomically(context, session_file)
            write_meta(
                session_file,
                build_meta(
                    username=final_handle,
                    user_id=cookies.get("ds_user_id"),
                    locale=locale,
                    timezone_id=timezone_id,
                    viewport=VIEWPORT,
                ),
            )
            return final_handle, session_file
        finally:
            browser.close()


def main() -> int:
    args = _parse_args()

    raw_handle = (args.username or cfg.instagram_username).strip()
    if not raw_handle:
        console.print(
            "[red]INSTAGRAM_USERNAME not set in .env and --username not provided.[/red]"
        )
        return 2

    try:
        handle = validate_handle(raw_handle)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    others = sorted(
        p.name for p in cfg.session_dir.glob(SESSION_GLOB)
        if p.name.casefold() != f"{handle}_instagram_state.json".casefold()
    )
    if others:
        console.print(
            f"[cyan]Other accounts in {cfg.session_dir} are left untouched: "
            f"{', '.join(others)}[/cyan]"
        )
    try:
        handle, session_file = _launch_and_save(
            args.headless, args.timeout, handle, cfg.session_dir,
            locale=args.locale, timezone_id=args.timezone_id,
        )
    except (RuntimeError, TimeoutError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    console.print(f"[green]Wrote storage state for {handle} to {session_file}[/green]")
    if others:
        console.print(
            "[green]Several accounts are now logged in: tools take `account`; set "
            "INSTAGRAM_DEFAULT_ACCOUNT for calls without it.[/green]"
        )
    console.print(
        "[green]Done. Register the server with Claude if you haven't: "
        "see 'Connect Claude' in the README.[/green]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
