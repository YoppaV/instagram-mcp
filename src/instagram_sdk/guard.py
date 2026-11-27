"""Classify a navigation or a fetch response as an Instagram blocking signal.

Pure functions, no Playwright. The first matching rule wins and the order
matters: Instagram's typical soft block is
``{"message": "Please wait a few minutes...", "require_login": true}``, which
is a rate limit, not a dead session — so the wait phrases are checked before
``require_login``.

Text is only ever matched in structured fields (``message``,
``feedback_message``, ``errors[].message``), never in the whole body: a caption
can say "try again later". Only a non-JSON (HTML) answer is scanned, and only
its first characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WAIT_PHRASES: tuple[str, ...] = (
    "try again later",
    "please wait a few minutes",
    "rate limit",
    "temporarily blocked",
)
TEXT_HEAD_CHARS = 2000

# URL fragments, in rule order (7-11 of the design table).
_URL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("/challenge/",), "challenge"),
    (("/accounts/suspended/",), "suspended"),
    (("/auth_platform/",), "auth_platform"),
    (("/consent/", "/privacy/checks/", "/terms/unblock/"), "consent"),
    (("/accounts/login",), "session_expired"),
)

SESSION_EXPIRED = "session_expired"
# Kinds that only time heals; everything else needs a person.
COOLDOWN_KINDS = frozenset({"rate_limited", "feedback_required"})


@dataclass(frozen=True)
class BlockSignal:
    kind: str
    detail: str


def _messages(body: dict[str, Any]) -> list[str]:
    out: list[str] = []
    # errorSummary/errorDescription: Facebook's error envelope ("for (;;);{...}").
    for key in ("message", "feedback_message", "errorSummary", "errorDescription"):
        value = body.get(key)
        if isinstance(value, str):
            out.append(value)
    errors = body.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                out.append(err["message"])
    return out


def _wait_phrase(texts: list[str]) -> str | None:
    for text in texts:
        low = text.lower()
        for phrase in WAIT_PHRASES:
            if phrase in low:
                return text[:200]
    return None


def _check_body(body: dict[str, Any]) -> BlockSignal | None:
    found = _wait_phrase(_messages(body))
    if found:
        return BlockSignal("rate_limited", found)
    message = body.get("message") if isinstance(body.get("message"), str) else ""
    if message == "feedback_required" or body.get("spam") is True:
        return BlockSignal("feedback_required", message or "spam")
    if (
        message in ("checkpoint_required", "challenge_required")
        or "checkpoint_url" in body
        or "challenge" in body
    ):
        return BlockSignal("checkpoint", message or "challenge")
    if message == "consent_required":
        return BlockSignal("consent", message)
    if message == "login_required" or body.get("require_login") is True:
        return BlockSignal("login_required", message or "require_login")
    return None


def _check_url(final_url: str) -> BlockSignal | None:
    url = final_url or ""
    for fragments, kind in _URL_RULES:
        for fragment in fragments:
            if fragment in url:
                return BlockSignal(kind, fragment)
    return None


def check_navigation(
    final_url: str,
    status: int | None,
    *,
    body: dict[str, Any] | None = None,
    text_head: str = "",
) -> BlockSignal | None:
    """Signal for a page navigation (``page.url`` after ``goto``).

    ``body``/``text_head`` are only given for error statuses: an HTML page
    that loaded fine is never scanned for phrases.
    """
    if status == 429:
        return BlockSignal("rate_limited", "HTTP 429")
    if isinstance(body, dict):
        signal = _check_body(body)
        if signal is not None:
            return signal
    elif text_head:
        found = _wait_phrase([text_head[:TEXT_HEAD_CHARS]])
        if found:
            return BlockSignal("rate_limited", "wait phrase in HTML")
    return _check_url(final_url)


def check_fetch(
    status: int,
    final_url: str,
    redirected: bool,
    content_type: str,
    body: Any | None,
    text_head: str,
) -> BlockSignal | None:
    """Signal for an in-page ``fetch`` response.

    ``body`` is the parsed JSON (``None`` if it was not JSON); ``text_head`` the
    start of the raw text, scanned only when there is no JSON.
    """
    if status == 429:
        return BlockSignal("rate_limited", "HTTP 429")
    if isinstance(body, dict):
        signal = _check_body(body)
        if signal is not None:
            return signal
    elif body is None:
        found = _wait_phrase([(text_head or "")[:TEXT_HEAD_CHARS]])
        if found:
            return BlockSignal("rate_limited", "wait phrase in HTML")
    return _check_url(final_url)
