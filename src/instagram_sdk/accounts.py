"""Accounts: discovery, the default account, and everything persisted per account.

**Discovery.** Every ``<handle>_instagram_state.json`` in ``INSTAGRAM_SESSION_DIR``
(or, if unset, ``~/.instagram-mcp/sessions/`` and then the legacy in-repo
``sessions/``) is one account. ``INSTAGRAM_SESSION_FILE`` adds one more. The
list is recomputed on every call, so a login needs no restart.

**Default account** (``AccountRegistry.default``), in order:

1. ``INSTAGRAM_DEFAULT_ACCOUNT`` — must have a session (a wrong explicit
   value is an error, never silently replaced);
2. ``INSTAGRAM_SESSION_FILE``;
3. ``INSTAGRAM_USERNAME`` when it has a session (legacy alias);
4. the only session there is — with a warning if ``INSTAGRAM_USERNAME``
   names some other account;
5. none → ``NoSessionError``; several → ``AmbiguousAccountError``. Never the
   first one in alphabetical order.

**Persistent state** under ``INSTAGRAM_STATE_DIR``::

    accounts/<handle>.json  {status, block, history, failures, budget, last_request_at}
    global.json             {last_account, last_request_at,
                             global_cooldown_until, global_cooldown_source}

Block rules (decision D7):

* ``rate_limited`` / ``feedback_required`` → ``cooldown`` for 12 h; a repeat
  within 48 h doubles the previous one, up to 72 h. Time lifts it.
* ``session_expired`` → ``expired``, tied to the session file's fingerprint.
* Anything else (challenge, checkpoint, suspended, consent, login_required,
  auth_platform) → ``blocked`` until a person clears it: a new session file
  (re-login) or ``python -m scripts.account_state --clear <handle>``.
* Any Instagram signal except an expired session also pauses the **other**
  accounts for 30 min (``global_cooldown``): they share the IP and browser.
* Three ``OperationFailedError`` in a row within 10 min → 15 min ``cooldown``
  with ``kind="failures"``: a run of unexplained failures can be a soft block.

Nothing here talks to the network.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .errors import (
    AccountBlockedError,
    AccountConfigError,
    AmbiguousAccountError,
    BudgetExceededError,
    NoSessionError,
    SessionExpiredError,
    StateUnavailableError,
    UnknownAccountError,
)
from .guard import COOLDOWN_KINDS, SESSION_EXPIRED, BlockSignal

if TYPE_CHECKING:
    from .settings import Settings

log = logging.getLogger("instagram_sdk")

_SESSION_SUFFIX = "_instagram_state"
_META_SUFFIX = "_instagram_meta"
SESSION_GLOB = "*_instagram_state.json"
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

COOLDOWN_S = 12 * 3600
COOLDOWN_REPEAT_WINDOW_S = 48 * 3600
COOLDOWN_MAX_S = 72 * 3600
GLOBAL_COOLDOWN_S = 30 * 60
GLOBAL_COOLDOWN_KIND = "global_cooldown"
FAILURE_WINDOW_S = 10 * 60
FAILURE_STRIKES = 3
FAILURE_COOLDOWN_S = 15 * 60
FAILURE_KIND = "failures"

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_COOLDOWN = "cooldown"
STATUS_EXPIRED = "expired"

LOGIN_HINT = "Run: python -m scripts.auth_login --username <handle>"


# --------------------------------------------------------------------------
# Handles and files
# --------------------------------------------------------------------------
def is_valid_handle(handle: str) -> bool:
    """``[A-Za-z0-9._]{1,30}`` and not only dots (it names a folder)."""
    return bool(_HANDLE_RE.match(handle)) and bool(handle.strip("."))


def normalize_handle(raw: str) -> str:
    return raw.strip().lstrip("@").casefold()


def session_fingerprint(path: Path) -> list[Any] | None:
    """``[resolved path, mtime_ns]`` of a session file, ``None`` if unreadable.

    The path is resolved so the same file reached through a symlink or a
    non-normalised path keeps the same fingerprint (and its ``blocked``).
    """
    try:
        real = path.resolve()
        return [str(real), real.stat().st_mtime_ns]
    except (OSError, RuntimeError):
        return None


def handle_for_session(path: Path, fallback: str = "default") -> str:
    """Handle from ``<handle>_instagram_state.json``; ``fallback`` otherwise."""
    stem = path.stem
    if stem.endswith(_SESSION_SUFFIX):
        handle = stem[: -len(_SESSION_SUFFIX)]
        if is_valid_handle(handle):
            return handle
    return fallback


def meta_path_for(session_path: Path) -> Path:
    """The sidecar next to a session file: ``<handle>_instagram_meta.json``."""
    stem = session_path.stem
    if stem.endswith(_SESSION_SUFFIX):
        stem = stem[: -len(_SESSION_SUFFIX)]
    return session_path.with_name(f"{stem}{_META_SUFFIX}.json")


def read_session_user_id(path: Path) -> str | None:
    """``ds_user_id`` cookie from a storage_state file, without network."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for cookie in (raw.get("cookies") or []) if isinstance(raw, dict) else []:
        if isinstance(cookie, dict) and cookie.get("name") == "ds_user_id":
            value = cookie.get("value")
            return str(value) if value else None
    return None


def read_meta(session_path: Path) -> dict[str, Any]:
    """The session's sidecar, or ``{}`` (old sessions have none)."""
    try:
        data = json.loads(meta_path_for(session_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build_meta(
    *,
    username: str,
    user_id: str | None,
    locale: str,
    timezone_id: str,
    viewport: Mapping[str, int],
    created_at: float | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    when = datetime.fromtimestamp(created_at or time.time(), tz=timezone.utc)
    return {
        "version": 1,
        "user_id": user_id,
        "username": username,
        "created_at": when.isoformat(timespec="seconds"),
        "locale": locale,
        "timezone_id": timezone_id,
        "viewport": {"width": int(viewport["width"]), "height": int(viewport["height"])},
    }


def write_meta(session_path: Path, meta: Mapping[str, Any]) -> Path:
    """Write the sidecar (no secrets in it). Returns its path."""
    path = meta_path_for(session_path)
    atomic_write_json(path, dict(meta))
    return path


@dataclass(frozen=True)
class Account:
    handle: str
    session_path: Path
    user_id: str | None = field(default=None, compare=False)
    meta: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @classmethod
    def from_session(cls, path: Path, *, fallback: str = "default") -> "Account":
        meta = read_meta(path)
        # The sidecar's id (recorded at login) first: the ds_user_id cookie
        # may be expired even while the session works.
        meta_id = meta.get("user_id")
        return cls(
            handle=handle_for_session(path, fallback),
            session_path=path,
            user_id=str(meta_id) if meta_id else read_session_user_id(path),
            meta=meta,
        )

    @property
    def key(self) -> str:
        return self.handle.casefold()

    def fingerprint_options(self, *, locale: str, timezone_id: str,
                            viewport: Mapping[str, int]) -> dict[str, Any]:
        """``locale``/``timezone_id``/``viewport`` for this account's context.

        The sidecar written at login wins; the environment is the fallback.
        """
        meta = self.meta or {}
        loc = meta.get("locale") if isinstance(meta.get("locale"), str) else None
        tz = meta.get("timezone_id") if isinstance(meta.get("timezone_id"), str) else None
        vp = meta.get("viewport")
        if not (isinstance(vp, dict) and isinstance(vp.get("width"), int)
                and isinstance(vp.get("height"), int)):
            vp = None
        return {
            "locale": loc or locale,
            "timezone_id": tz or timezone_id,
            "viewport": dict(vp or viewport),
        }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DefaultChoice:
    account: Account
    source: str  # default_account | session_file | username | only_session | fixed
    warning: str | None = None


class AccountRegistry:
    """Which accounts exist and which one a call without ``account`` uses.

    ``fixed`` turns it into a one-account registry (the library facade
    ``BrowserSession`` passes the session file it was given).
    """

    def __init__(self, settings: "Settings", *, fixed: Callable[[], Path] | None = None) -> None:
        self._settings = settings
        self._fixed = fixed

    # ------------------------------------------------------------------
    def list(self) -> list[Account]:
        """All accounts, sorted by handle. Recomputed on every call."""
        if self._fixed is not None:
            return [Account.from_session(self._fixed())]
        found: dict[str, Account] = {}
        settings = self._settings
        if settings.session_file is not None:
            fallback = settings.username if is_valid_handle(settings.username) else "default"
            acct = Account.from_session(settings.session_file, fallback=fallback)
            found[acct.key] = acct
        for directory in settings.candidate_session_dirs():
            in_dir: dict[str, Path] = {}
            try:
                files = sorted(directory.glob(SESSION_GLOB))
            except OSError:
                continue
            for path in files:
                stem = path.stem[: -len(_SESSION_SUFFIX)]
                if not is_valid_handle(stem):
                    log.warning("instagram_sdk: ignoring session file with an invalid "
                                "handle: %s", path.name)
                    continue
                key = stem.casefold()
                if key in in_dir:
                    raise AccountConfigError(
                        f"Two session files differ only in case: {in_dir[key].name} and "
                        f"{path.name} in {directory}. Delete or rename one."
                    )
                in_dir[key] = path
                if key in found:
                    # Explicit INSTAGRAM_SESSION_FILE, or an earlier directory,
                    # shadows the same handle further down the list.
                    continue
                found[key] = Account.from_session(path)
        return sorted(found.values(), key=lambda a: a.key)

    def find(self, handle: str) -> Account | None:
        key = normalize_handle(handle)
        for acct in self.list():
            if acct.key == key:
                return acct
        return None

    def _available(self, accounts: list[Account]) -> str:
        if not accounts:
            return f"none — no session files found. {LOGIN_HINT}"
        return ", ".join(repr(a.handle) for a in accounts)

    def default(self) -> DefaultChoice:
        accounts = self.list()
        if self._fixed is not None:
            return DefaultChoice(accounts[0], "fixed")
        settings = self._settings
        by_key = {a.key: a for a in accounts}
        if settings.default_account:
            acct = by_key.get(normalize_handle(settings.default_account))
            if acct is None:
                raise UnknownAccountError(
                    f"INSTAGRAM_DEFAULT_ACCOUNT={settings.default_account!r} has no session "
                    f"file. Available: {self._available(accounts)}."
                )
            return DefaultChoice(acct, "default_account")
        if settings.session_file is not None:
            for acct in accounts:
                if acct.session_path == settings.session_file:
                    return DefaultChoice(acct, "session_file")
        warning = None
        if settings.username:
            acct = by_key.get(normalize_handle(settings.username))
            if acct is not None:
                return DefaultChoice(acct, "username")
            warning = (f"INSTAGRAM_USERNAME={settings.username} does not match any "
                       "session file")
        if len(accounts) == 1:
            return DefaultChoice(accounts[0], "only_session", warning)
        if not accounts:
            raise NoSessionError(
                "No Instagram session found "
                f"(looked in {', '.join(str(d) for d in settings.candidate_session_dirs())}). "
                f"{LOGIN_HINT}"
            )
        raise AmbiguousAccountError(
            f"Several Instagram accounts are logged in ({self._available(accounts)}) and "
            "none is the default. Pass `account`, or set INSTAGRAM_DEFAULT_ACCOUNT."
            + (f" Note: {warning}." if warning else "")
        )

    def resolve(self, account: str | None) -> Account:
        """The account a call runs as. Raises ``AccountError`` subclasses."""
        if account is None or not str(account).strip():
            return self.default().account
        wanted = normalize_handle(str(account))
        accounts = self.list()
        if not is_valid_handle(wanted):
            raise UnknownAccountError(
                f"{account!r} is not a valid account handle. "
                f"Available: {self._available(accounts)}."
            )
        for acct in accounts:
            if acct.key == wanted:
                return acct
        raise UnknownAccountError(
            f"Unknown account {account!r}. Available: {self._available(accounts)}."
        )


# --------------------------------------------------------------------------
# Persistent state
# --------------------------------------------------------------------------
def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
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


_atomic_write_json = atomic_write_json  # backwards-compatible alias

HOUR_S = 3600
DAY_S = 86400

# Per-class (per hour, per day) limits on top of ``general`` (design §10.2).
CLASS_LIMITS: dict[str, tuple[int, int]] = {
    "follow": (30, 100),
    "comments": (40, 200),
    "search": (30, 150),
}


class Budget:
    """Sliding hour/day windows over per-minute counters, per account.

    Every request counts in ``general``; a request of another class also
    counts in its own. Going over a limit raises ``BudgetExceededError``
    (never sleeps). It is our own cap, not an Instagram signal.
    """

    def __init__(
        self,
        buckets: Mapping[str, Mapping[str, int]] | None = None,
        *,
        per_hour: int,
        per_day: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self.limits: dict[str, tuple[int, int]] = {"general": (per_hour, per_day), **CLASS_LIMITS}
        self.buckets: dict[int, dict[str, int]] = {}
        for minute, counts in (buckets or {}).items():
            try:
                m = int(minute)
            except (TypeError, ValueError):
                continue
            if isinstance(counts, Mapping):
                self.buckets[m] = {str(k): int(v) for k, v in counts.items()
                                   if isinstance(v, (int, float))}
        self._prune()

    def _now_minute(self) -> int:
        return int(self._clock() // 60)

    def _prune(self) -> None:
        oldest = self._now_minute() - DAY_S // 60
        for m in [m for m in self.buckets if m <= oldest]:
            del self.buckets[m]

    def used(self, cls: str, window_s: int) -> int:
        start = self._now_minute() - window_s // 60
        return sum(c.get(cls, 0) for m, c in self.buckets.items() if m > start)

    def _retry_after(self, cls: str, window_s: int, limit: int, cost: int) -> float:
        now = self._clock()
        start = self._now_minute() - window_s // 60
        total = self.used(cls, window_s)
        for m in sorted(m for m in self.buckets if m > start):
            total -= self.buckets[m].get(cls, 0)
            if total + cost <= limit:
                return max(0.0, (m + 1) * 60 + window_s - now)
        return float(window_s)

    def charge(self, cls: str = "general", cost: int = 1) -> None:
        self._prune()
        classes = ["general"] + ([cls] if cls != "general" and cls in self.limits else [])
        for name in classes:
            per_hour, per_day = self.limits[name]
            for window, limit, label in ((HOUR_S, per_hour, "hour"), (DAY_S, per_day, "day")):
                if limit <= 0:
                    continue
                if self.used(name, window) + cost > limit:
                    wait = self._retry_after(name, window, limit, cost)
                    raise BudgetExceededError(
                        f"request budget for this account reached ({limit} '{name}' requests "
                        f"per {label}); retry in about {math.ceil(wait / 60)} min",
                        retry_after_s=wait,
                    )
        bucket = self.buckets.setdefault(self._now_minute(), {})
        for name in classes:
            bucket[name] = bucket.get(name, 0) + cost

    def to_json(self) -> dict[str, dict[str, int]]:
        self._prune()
        return {str(m): dict(c) for m, c in sorted(self.buckets.items())}


class AccountStateStore:
    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = state_dir
        self._clock = clock
        # States that could not be written (read-only volume...). They win
        # over the disk until a write succeeds, so a block always applies.
        self._unsaved: dict[str, dict[str, Any]] = {}

    def _path(self, handle: str) -> Path:
        return self.state_dir / "accounts" / f"{handle.casefold()}.json"

    @property
    def global_path(self) -> Path:
        return self.state_dir / "global.json"

    @property
    def degraded(self) -> bool:
        """True if some state could only be kept in memory (a write failed)."""
        return bool(self._unsaved)

    def ensure_writable(self) -> None:
        """Probe that ``accounts/`` and the state root accept atomic writes."""
        for directory in (self.state_dir, self.state_dir / "accounts"):
            try:
                probe = directory / ".write_probe.json"
                atomic_write_json(probe, {})
                probe.unlink()
            except OSError as exc:
                raise StateUnavailableError(
                    f"State directory {directory} is not writable ({exc}). The block "
                    "state, request budget and cross-process lock live there, so no "
                    "Instagram call is made. Fix its permissions or point "
                    "INSTAGRAM_STATE_DIR elsewhere (INSTAGRAM_ALLOW_DEGRADED_STATE=1 "
                    "runs anyway with in-memory state, at your own risk)."
                ) from exc

    def _read(self, path: Path, key: str) -> dict[str, Any] | None:
        """``None`` only when the file does not exist. Any other failure
        raises ``StateUnavailableError``: an unreadable state must never read
        as "no block" (fail closed)."""
        pending = self._unsaved.get(key)
        if pending is not None:
            return dict(pending)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise StateUnavailableError(
                f"State file {path} is unreadable or corrupt ({exc}); refusing to "
                "assume the account is not blocked. Fix its permissions, or repair it "
                "with: python -m scripts.account_state --clear <handle>"
            ) from exc
        if not isinstance(data, dict):
            raise StateUnavailableError(
                f"State file {path} does not hold a JSON object; repair it with: "
                "python -m scripts.account_state --clear <handle>"
            )
        return data

    def _read_or_empty(self, path: Path, key: str) -> dict[str, Any]:
        try:
            return self._read(path, key) or {}
        except StateUnavailableError as exc:
            log.error("instagram_sdk: %s — starting that file over", exc)
            return {}

    def _write(self, path: Path, key: str, data: dict[str, Any]) -> None:
        """Persist ``data``; on a disk error keep it in memory and log."""
        try:
            atomic_write_json(path, data)
        except OSError as exc:
            self._unsaved[key] = dict(data)
            log.error("instagram_sdk: could not write state %s (%s); "
                      "keeping it in memory for this process", path.name, exc)
            return
        self._unsaved.pop(key, None)

    def load(self, handle: str) -> dict[str, Any]:
        data = self._read(self._path(handle), handle.casefold()) or {}
        data.setdefault("status", STATUS_OK)
        data.setdefault("block", None)
        return data

    def save(self, handle: str, data: dict[str, Any]) -> None:
        self._write(self._path(handle), handle.casefold(), data)

    def update(self, handle: str, **fields: Any) -> None:
        data = self.load(handle)
        data.update(fields)
        self.save(handle, data)

    def load_global(self) -> dict[str, Any]:
        return self._read(self.global_path, "\0global") or {}

    def save_global(self, data: dict[str, Any]) -> None:
        self._write(self.global_path, "\0global", data)

    def clear(self, handle: str) -> bool:
        """Reset an account to ``ok`` (and lift the global pause it caused).

        The manual repair path: a corrupt state file is started over. Writes
        raise ``OSError`` here instead of falling back to memory. True if
        there was something to clear.
        """
        key = handle.casefold()
        path = self._path(handle)
        try:
            before = self._read(path, key) or {}
            changed = (before.get("status", STATUS_OK) != STATUS_OK
                       or bool(before.get("failures")))
        except StateUnavailableError:
            before, changed = {}, True
        atomic_write_json(path, {**before, "status": STATUS_OK, "block": None, "failures": []})
        self._unsaved.pop(key, None)
        try:
            glob = self._read(self.global_path, "\0global") or {}
        except StateUnavailableError:
            glob, changed = {}, True
            atomic_write_json(self.global_path, glob)
        src = str(glob.get("global_cooldown_source") or "")
        if src.casefold() == key and glob.get("global_cooldown_until"):
            glob["global_cooldown_until"] = None
            glob["global_cooldown_source"] = None
            atomic_write_json(self.global_path, glob)
            changed = True
        self._unsaved.pop("\0global", None)
        return changed

    # ------------------------------------------------------------------
    def _global_hold(self, account: Account) -> dict[str, Any] | None:
        glob = self.load_global()
        until = glob.get("global_cooldown_until")
        src = str(glob.get("global_cooldown_source") or "")
        if not isinstance(until, (int, float)) or until <= self._clock():
            return None
        if src.casefold() == account.key:
            return None
        return {
            "kind": GLOBAL_COOLDOWN_KIND,
            "detail": (f"account {src!r} was stopped by Instagram; every account shares "
                       "this IP and browser, so all of them pause"),
            "at": None,
            "until": until,
        }

    def effective(self, account: Account) -> dict[str, Any]:
        """Current state, after applying expiry, session-file changes and the
        global pause."""
        data = self.load(account.handle)
        status = data.get("status", STATUS_OK)
        block = data.get("block") or {}
        if status == STATUS_COOLDOWN:
            until = block.get("until")
            if not isinstance(until, (int, float)) or until <= self._clock():
                data = {**data, "status": STATUS_OK, "block": None}
        elif status in (STATUS_BLOCKED, STATUS_EXPIRED):
            if block.get("session_fp") != session_fingerprint(account.session_path):
                data = {**data, "status": STATUS_OK, "block": None}
        if data.get("status", STATUS_OK) == STATUS_OK:
            hold = self._global_hold(account)
            if hold is not None:
                data = {**data, "status": STATUS_COOLDOWN, "block": hold}
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
        data = self._read_or_empty(self._path(account.handle), account.key)
        data.setdefault("status", STATUS_OK)
        history = [h for h in (data.get("history") or []) if isinstance(h, dict)]
        fp = session_fingerprint(account.session_path)
        block: dict[str, Any] = {
            "user_id": account.user_id,
            "kind": signal.kind,
            "detail": signal.detail,
            "at": now,
            "until": None,
            "session_fp": fp,
        }
        if signal.kind == SESSION_EXPIRED:
            status = STATUS_EXPIRED
            error: Exception = SessionExpiredError(
                f"Instagram session for {account.handle!r} expired — re-run: "
                f"python -m scripts.auth_login --username {account.handle}"
            )
        elif signal.kind in COOLDOWN_KINDS:
            status = STATUS_COOLDOWN
            recent = [
                h for h in history
                if h.get("cooldown") and h.get("kind") != FAILURE_KIND
                and now - float(h.get("at", 0)) < COOLDOWN_REPEAT_WINDOW_S
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
        self.save(account.handle, {**data, "status": status, "block": block,
                                   "history": history[-20:], "failures": []})
        if signal.kind != SESSION_EXPIRED:
            glob = self._read_or_empty(self.global_path, "\0global")
            glob["global_cooldown_until"] = now + GLOBAL_COOLDOWN_S
            glob["global_cooldown_source"] = account.handle
            self.save_global(glob)
        log.warning("instagram_sdk: account %s %s (%s)", account.handle, status, signal.kind)
        return error

    def record_failure(self, account: Account, detail: str) -> bool:
        """Count an ``OperationFailedError``. True if it tripped the brake."""
        now = self._clock()
        data = self.load(account.handle)
        failures = [f for f in (data.get("failures") or [])
                    if isinstance(f, (int, float)) and now - f < FAILURE_WINDOW_S]
        failures.append(now)
        if len(failures) < FAILURE_STRIKES:
            self.save(account.handle, {**data, "failures": failures})
            return False
        history = [h for h in (data.get("history") or []) if isinstance(h, dict)]
        history.append({"kind": FAILURE_KIND, "at": now, "cooldown": True,
                        "duration_s": FAILURE_COOLDOWN_S})
        block = {
            "kind": FAILURE_KIND,
            "detail": f"{FAILURE_STRIKES} failed operations in a row ({detail[:200]})",
            "at": now,
            "until": now + FAILURE_COOLDOWN_S,
            "duration_s": FAILURE_COOLDOWN_S,
            "session_fp": session_fingerprint(account.session_path),
        }
        self.save(account.handle, {**data, "status": STATUS_COOLDOWN, "block": block,
                                   "failures": [], "history": history[-20:]})
        log.warning("instagram_sdk: account %s paused %ss after repeated failures",
                    account.handle, FAILURE_COOLDOWN_S)
        return True

    def record_success(self, account: Account) -> None:
        data = self.load(account.handle)
        if data.get("failures"):
            self.save(account.handle, {**data, "failures": []})

    # ------------------------------------------------------------------
    def budget(self, account: Account, *, per_hour: int, per_day: int) -> Budget:
        return Budget(self.load(account.handle).get("budget") or {},
                      per_hour=per_hour, per_day=per_day, clock=self._clock)

    def save_usage(self, account: Account, budget: Budget | None,
                   last_request_at: float | None) -> None:
        """Persist the budget counters and the last request time (account + global)."""
        data = self.load(account.handle)
        if budget is not None:
            data["budget"] = budget.to_json()
        if last_request_at is not None:
            data["last_request_at"] = last_request_at
        self.save(account.handle, data)
        if last_request_at is not None:
            glob = self.load_global()
            glob["last_account"] = account.key
            glob["last_request_at"] = last_request_at
            self.save_global(glob)

    def summary(self, account: Account) -> dict[str, Any]:
        try:
            data = self.effective(account)
        except StateUnavailableError as exc:
            return {"state": "unknown",
                    "block": {"kind": "state_unavailable", "detail": str(exc), "until": None}}
        block = data.get("block")
        if block:
            block = {k: v for k, v in block.items() if k != "session_fp"}
        return {"state": data.get("status", STATUS_OK), "block": block or None}
