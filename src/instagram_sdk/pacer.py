"""Pauses between requests and the per-call request cap.

Principle: the request rate is never faster than the old scroll loop. Every
data page used to cost one scroll with a 3.0-6.5 s pause; that pause is kept
between consecutive requests. What the in-page fetch saves is RAM and passive
waiting, not burst speed.

These constants are a danger zone (they replace ``scraper.py``'s): do not
loosen them without asking.

The hourly/daily budget persisted per account arrives with multi-account
support; this PR only has the pauses and the per-call cap.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable

from .errors import BudgetExceededError

PRE_REQUEST_PAUSE_S: tuple[float, float] = (1.5, 3.0)
BETWEEN_REQUESTS_PAUSE_S: tuple[float, float] = (3.0, 6.5)
RETRY_PAUSE_S: tuple[float, float] = (5.0, 10.0)
MIN_GAP_S = 1.5
MAX_REQUESTS_PER_CALL = 20


class Pacer:
    """One per account context. ``new_call()`` marks the start of a tool call."""

    def __init__(
        self,
        *,
        max_requests_per_call: int | None = MAX_REQUESTS_PER_CALL,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.max_requests_per_call = max_requests_per_call
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self._uniform = uniform
        self._last_request_at: float | None = None
        self.requests_this_call = 0
        self._first_of_call = True

    def new_call(self) -> None:
        self.requests_this_call = 0
        self._first_of_call = True

    def _check_cap(self, cost: int = 1) -> None:
        cap = self.max_requests_per_call
        if cap is not None and self.requests_this_call + cost > cap:
            raise BudgetExceededError(
                f"per-call cap of {cap} Instagram requests reached; "
                "ask for a smaller limit or continue in another call",
                retry_after_s=0.0,
            )

    async def before_request(self, budget_class: str = "general") -> None:
        """Wait the right pause, then count the request. Raises on the cap."""
        self._check_cap()
        rng = PRE_REQUEST_PAUSE_S if self._first_of_call else BETWEEN_REQUESTS_PAUSE_S
        pause = self._uniform(*rng)
        if self._last_request_at is not None:
            since = self._clock() - self._last_request_at
            pause = max(pause, MIN_GAP_S - since)
        if pause > 0:
            await self._sleep(pause)
        self.requests_this_call += 1
        self._first_of_call = False
        self._last_request_at = self._clock()

    async def retry_pause(self) -> None:
        await self._sleep(self._uniform(*RETRY_PAUSE_S))
