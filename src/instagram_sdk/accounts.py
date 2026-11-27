"""Accounts and their persistent block state.

This PR serves a single account (the one session file the server resolves);
the registry of several accounts arrives later. What already lives here is
the part that must survive a restart: whether Instagram stopped the account.

State file: ``<INSTAGRAM_STATE_DIR>/accounts/<handle>.json``::

    {"status": "ok|blocked|cooldown|expired",
     "block": {"kind", "detail", "at", "until", "duration_s", "session_fp", "strikes"}}

Rules (decision D7):

* ``rate_limited`` / ``feedback_required`` → ``cooldown`` for 12 h; a repeat
  within 48 h doubles the previous one, up to 72 h. Time lifts it.
* ``session_expired`` → ``expired``, tied to the session file's fingerprint.
* Anything else (challenge, checkpoint, suspended, consent, login_required,
  auth_platform) → ``blocked`` until a person clears it: a new session file
  (re-login) or ``python -m scripts.account_state --clear <handle>``.

Nothing here talks to the network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import AccountBlockedError, SessionExpiredError
from .guard import COOLDOWN_KINDS, SESSION_EXPIRED, BlockSignal

log = logging.getLogger("instagram_sdk")

_SESSION_SUFFIX = "_instagram_state"
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

COOLDOWN_S = 12 * 3600
COOLDOWN_REPEAT_WINDOW_S = 48 * 3600
COOLDOWN_MAX_S = 72 * 3600

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_COOLDOWN = "cooldown"
STATUS_EXPIRED = "expired"


def session_fingerprint(path: Path) -> list[Any] | None:
    """``[path, mtime_ns]`` of a session file, ``None`` if unreadable."""
    try:
        return [str(path), path.stat().st_mtime_ns]
    except OSError:
        return None


def handle_for_session(path: Path) -> str:
    """Handle from ``<handle>_instagram_state.json``; ``"default"`` otherwise."""
    stem = path.stem
    if stem.endswith(_SESSION_SUFFIX):
        handle = stem[: -len(_SESSION_SUFFIX)]
        if _HANDLE_RE.match(handle) and handle.strip("."):
            return handle
    return "default"


@dataclass(frozen=True)
class Account:
    handle: str
    session_path: Path

    @classmethod
    def from_session(cls, path: Path) -> "Account":
        return cls(handle=handle_for_session(path), session_path=path)

    @property
    def key(self) -> str:
        return self.handle.casefold()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AccountStateStore:
    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = state_dir
        self._clock = clock

    def _path(self, handle: str) -> Path:
        return self.state_dir / "accounts" / f"{handle.casefold()}.json"

    def load(self, handle: str) -> dict[str, Any]:
        path = self._path(handle)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"status": STATUS_OK, "block": None}
        except (OSError, ValueError):
            log.warning("instagram_sdk: unreadable account state %s; ignoring", path)
            return {"status": STATUS_OK, "block": None}
        if not isinstance(data, dict):
            return {"status": STATUS_OK, "block": None}
        data.setdefault("status", STATUS_OK)
        data.setdefault("block", None)
        return data

    def save(self, handle: str, data: dict[str, Any]) -> None:
        _atomic_write_json(self._path(handle), data)

    def clear(self, handle: str) -> bool:
        """Reset an account to ``ok``. True if there was something to clear."""
        before = self.load(handle)
        self.save(handle, {"status": STATUS_OK, "block": None,
                           "history": before.get("history") or []})
        return before.get("status") != STATUS_OK

    # ------------------------------------------------------------------
    def effective(self, account: Account) -> dict[str, Any]:
        """Current state, after applying expiry and session-file changes."""
        data = self.load(account.handle)
        status = data.get("status", STATUS_OK)
        block = data.get("block") or {}
        if status == STATUS_COOLDOWN:
            until = block.get("until")
            if not isinstance(until, (int, float)) or until <= self._clock():
                return {**data, "status": STATUS_OK, "block": None}
        if status in (STATUS_BLOCKED, STATUS_EXPIRED):
            if block.get("session_fp") != session_fingerprint(account.session_path):
                return {**data, "status": STATUS_OK, "block": None}
        return data

    def check(self, account: Account) -> None:
        """Raise the stored error, without any network, if the account is held."""
        data = self.effective(account)
        status = data.get("status")
        block = data.get("block") or {}
        if status == STATUS_EXPIRED:
            raise SessionExpiredError(
                f"Instagram session for {account.handle!r} expired earlier — "
                f"re-run: python -m scripts.auth_login --username {account.handle}"
            )
        if status in (STATUS_BLOCKED, STATUS_COOLDOWN):
            raise AccountBlockedError(
                account.handle,
                block.get("kind", "unknown"),
                block.get("detail", ""),
                block.get("until") if status == STATUS_COOLDOWN else None,
            )

    def record(self, account: Account, signal: BlockSignal) -> Exception:
        """Persist ``signal`` and return the error to raise for it."""
        now = self._clock()
        data = self.load(account.handle)
        history = [h for h in (data.get("history") or []) if isinstance(h, dict)]
        fp = session_fingerprint(account.session_path)
        block: dict[str, Any] = {
            "kind": signal.kind,
            "detail": signal.detail,
            "at": now,
            "until": None,
            "session_fp": fp,
        }
        if signal.kind == SESSION_EXPIRED:
            status = STATUS_EXPIRED
            error: Exception = SessionExpiredError(
                f"Instagram session expired — re-run: "
                f"python -m scripts.auth_login --username {account.handle}"
            )
        elif signal.kind in COOLDOWN_KINDS:
            status = STATUS_COOLDOWN
            recent = [
                h for h in history
                if h.get("cooldown") and now - float(h.get("at", 0)) < COOLDOWN_REPEAT_WINDOW_S
            ]
            duration = COOLDOWN_S
            if recent:
                duration = min(COOLDOWN_MAX_S, int(recent[-1].get("duration_s", COOLDOWN_S)) * 2)
            block["until"] = now + duration
            block["duration_s"] = duration
            block["strikes"] = len(recent) + 1
            error = AccountBlockedError(account.handle, signal.kind, signal.detail, block["until"])
        else:
            status = STATUS_BLOCKED
            error = AccountBlockedError(account.handle, signal.kind, signal.detail, None)
        history.append({
            "kind": signal.kind,
            "at": now,
            "cooldown": status == STATUS_COOLDOWN,
            "duration_s": block.get("duration_s"),
        })
        self.save(account.handle, {"status": status, "block": block, "history": history[-20:]})
        log.warning("instagram_sdk: account %s %s (%s)", account.handle, status, signal.kind)
        return error

    def summary(self, account: Account) -> dict[str, Any]:
        data = self.effective(account)
        block = data.get("block")
        if block:
            block = {k: v for k, v in block.items() if k != "session_fp"}
        return {"state": data.get("status", STATUS_OK), "block": block or None}
