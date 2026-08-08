"""
Phase 1 rate-limit stub — SUPERSEDED by app/ratelimit/limiter.py as of
Phase 2. `app/api/v1_chat.py` no longer imports this file.

Retained (not deleted) for two reasons: it's the cleanest possible
reference for the interface `RateLimiter` had to preserve (`check()` /
`reconcile()` — Phase 2's real implementation keeps the same method
signatures, per the Phase 1 TRD note: "the route handler never has to
change, only the object it calls"), and it remains a handy always-allow
test double for tests that want to exercise something else (e.g.
streaming disconnect handling in test_streaming_passthrough.py) without
also standing up Redis/fakeredis.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int | None = None
    remaining_rpm: int | None = None
    remaining_tpm: int | None = None


class RateLimiter:
    """Always allows. No Redis, no state, no per-team accounting — Phase 1 behavior."""

    async def check(
        self, *, team_id: str, estimated_tokens: int, priority: str
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=True)

    async def reconcile(self, *, team_id: str, reserved_tokens: int, actual_tokens: int) -> None:
        return None
