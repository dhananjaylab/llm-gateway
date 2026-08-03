"""
Phase 1 circuit-breaker stub.

Always reports Closed (i.e. "allow the call"). Phase 3 replaces
`CircuitBreaker` with a Redis-backed three-state state machine
(circuit:{provider}:{model}, per Document 05) implementing this same
`allow_request` / `record_success` / `record_failure` signature, so
app/api/v1_chat.py's call sites do not change shape — only which object
they're calling.

No retry-with-backoff or fallback-chain resolution exists in Phase 1
either; a failed provider call simply bubbles up as an error response.
Phase 3 adds app/resilience/fallback.py and app/resilience/health.py
alongside this file.
"""

from __future__ import annotations


class CircuitBreaker:
    """Always Closed. No failure tracking, no cooldown, no half-open probe yet."""

    async def allow_request(self, *, provider: str, model: str) -> bool:
        return True

    async def record_success(self, *, provider: str, model: str) -> None:
        return None

    async def record_failure(self, *, provider: str, model: str) -> None:
        return None
