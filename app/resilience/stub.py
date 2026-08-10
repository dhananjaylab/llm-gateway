"""
Phase 1 circuit-breaker stub — SUPERSEDED by app/resilience/circuit_breaker.py
as of Phase 3. `app/api/v1_chat.py` no longer imports this file.

Retained (not deleted) for the same two reasons app/ratelimit/stub.py was
kept after Phase 2: it's the cleanest possible reference for the
interface `CircuitBreaker` had to preserve (`allow_request()` /
`record_success()` / `record_failure()` — Phase 3's real implementation
keeps the same method signatures, per this file's own original docstring:
"app/api/v1_chat.py's call sites do not change shape — only which object
they're calling"), and it remains a handy always-Closed test double for
tests that want to exercise something else without also standing up
Redis/fakeredis for circuit state.
"""

from __future__ import annotations


class CircuitBreaker:
    """Always Closed. No failure tracking, no cooldown, no half-open probe."""

    async def allow_request(self, *, provider: str, model: str) -> bool:
        return True

    async def record_success(self, *, provider: str, model: str) -> None:
        return None

    async def record_failure(self, *, provider: str, model: str) -> None:
        return None
