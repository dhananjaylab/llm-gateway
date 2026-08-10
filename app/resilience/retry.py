"""
Retry with exponential backoff (TRD, "Transient vs. Permanent Error
Classification and Exponential Backoff"; Document 05's Full-Jitter
formula).

Hand-rolled rather than `tenacity` — considered during Phase 3 planning
(tenacity is current and well-maintained), but this codebase already has
an established, tested pattern for exactly this shape of problem
(RateLimiter's injectable `clock=time.time` in app/ratelimit/limiter.py):
a small class with an injectable time-control seam so tests can assert
backoff behavior without real wall-clock waits. `RetryPolicy` needs to
sit directly between the circuit breaker and the fallback chain walk
(app/resilience/fallback.py) anyway, reading `ProviderError.retryable` to
decide retry-vs-abort — a hand-rolled ~40 lines keeps that decision in
one obvious place rather than mapped through a third-party predicate/
wait-strategy API, and avoids a new dependency for something this small.

Formula (Document 05): delay for retry attempt n is
`random(0, min(max_delay, base_delay * 2^(n-1)))` — "full jitter", the
AWS Architecture Blog's recommended variant, which the TRD's own
"Randomizing the backoff interval prevents thundering herd problems"
note is describing.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.providers.base import ProviderError

logger = logging.getLogger("gateway.retry")

T = TypeVar("T")


class RetryPolicy:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.2,
        max_delay_seconds: float = 4.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._max_attempts = max(1, max_attempts)
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._sleep = sleep
        self._rng = rng

    def compute_delay(self, attempt: int) -> float:
        """`attempt` is 1-indexed: 1 = the delay before the *first* retry."""
        cap = min(self._max_delay, self._base_delay * (2 ** (attempt - 1)))
        return self._rng() * cap

    async def run(self, fn: Callable[[], Awaitable[T]], *, description: str = "") -> T:
        """
        Calls `fn()` up to `max_attempts` times against a single target
        (one provider-model — this policy does not know about fallback
        chains at all; app/resilience/fallback.py composes this with the
        chain walk).

        Retries only a `ProviderError` whose `.retryable` is True, per
        Document 03's edge case table: "Non-retryable error... Aborts
        immediately... never retried, never triggers fallback." On the
        final exhausted attempt, or on any non-retryable error, the
        original `ProviderError` is re-raised as-is (its `.retryable`
        flag intact) so the caller can distinguish "give up on this link,
        try the next one" from "give up on the whole chain" without this
        class needing to know anything about chains.
        """
        last_error: ProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await fn()
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                if attempt >= self._max_attempts:
                    raise
                delay = self.compute_delay(attempt)
                logger.info(
                    "retrying %s after transient error (attempt %d/%d, backing off %.3fs): %s",
                    description or "provider call",
                    attempt,
                    self._max_attempts,
                    delay,
                    exc.message,
                    extra={
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "delay_seconds": round(delay, 4),
                        "error_type": exc.error_type,
                    },
                )
                await self._sleep(delay)
        # Unreachable (the loop always returns or raises), but keeps
        # static type-checkers and defensive readers happy.
        assert last_error is not None
        raise last_error
