"""Typed errors raised by the SDK.

Every failure a tool can surface is one of these, so a caller (or the model
reading the MCP error) can tell "Instagram is throttling this account" apart
from "the session is gone", "our own budget ran out" or "the response changed
shape". ``AccountBlockedError`` and ``SessionExpiredError`` are deliberately
siblings: catching one never swallows the other.
"""

from __future__ import annotations

_RELOGIN = "python -m scripts.auth_login --username {handle}"
_CLEAR = "python -m scripts.account_state --clear {handle}"


class InstagramError(RuntimeError):
    """Base class for every error the SDK raises on purpose."""


class SessionExpiredError(InstagramError):
    """No valid session: the file is missing or Instagram sent us to login."""


class AccountBlockedError(InstagramError):
    """Instagram is holding the account back (rate limit, challenge, ...).

    ``kind`` is the ``guard`` signal (``rate_limited``, ``checkpoint``...);
    ``until`` is a unix timestamp for a timed cooldown, ``None`` when it needs
    a person to clear it. Nothing retries after this error.
    """

    def __init__(
        self,
        account: str,
        kind: str,
        detail: str = "",
        until: float | None = None,
    ) -> None:
        self.account = account
        self.kind = kind
        self.detail = detail
        self.until = until
        super().__init__(self._message())

    def _message(self) -> str:
        head = f"Account {self.account!r} stopped by Instagram ({self.kind})."
        if self.detail:
            head += f" Detail: {self.detail}."
        if self.until is not None:
            from datetime import datetime, timezone

            when = datetime.fromtimestamp(self.until, tz=timezone.utc)
            return (
                f"{head} Not retrying. Cooling down until "
                f"{when.isoformat(timespec='minutes')}; calls fail fast until then "
                f"(clear early with: {_CLEAR.format(handle=self.account)})."
            )
        return (
            f"{head} Not retrying. Open instagram.com in your own browser, "
            f"resolve the notice, then re-run "
            f"{_RELOGIN.format(handle=self.account)} "
            f"(or clear the flag with: {_CLEAR.format(handle=self.account)})."
        )


class BudgetExceededError(InstagramError):
    """Our own request cap was hit. Not an Instagram signal."""

    def __init__(self, message: str, retry_after_s: float = 0.0) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(message)


class OperationFailedError(InstagramError):
    """An operation failed without any blocking signal."""

    def __init__(self, op: str, detail: str) -> None:
        self.op = op
        self.detail = detail
        super().__init__(f"Instagram operation {op!r} failed: {detail}")


class DocIdStaleError(OperationFailedError):
    """GraphQL error or missing root: the ``doc_id`` probably rotated."""


class TokenError(OperationFailedError):
    """Page tokens (``fb_dtsg``/``lsd``) missing or rejected."""


class EndpointGoneError(OperationFailedError):
    """A REST path answers 404 HTML: Instagram retired it."""


class BrowserUnavailableError(InstagramError):
    """Chromium could not start, or died mid-call.

    ``retryable`` is True when relaunching may help (the browser crashed);
    False when it never started (binary missing, system libs).
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(message)


class AccountError(InstagramError, ValueError):
    """The requested account is not usable (unknown, ambiguous, no session)."""


class UnknownAccountError(AccountError):
    pass


class NoSessionError(AccountError):
    pass


class AmbiguousAccountError(AccountError):
    """Several sessions and nothing says which one is the default."""


class AccountConfigError(AccountError):
    """The session files on disk are inconsistent (e.g. two handles that only
    differ in case)."""
