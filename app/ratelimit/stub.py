"""
Phase 1 rate-limit stub.

Per the TRD's Concise Implementation Guide: "Stub rate limiting and circuit
breakers as permissive no-ops in this phase (always allow, always Closed) —
real logic arrives in Phases 2-3. Do not skip building their call sites;
wire the interface now so Phase 2 is a drop-in replacement, not a refactor."

Phase 2 replaces `RateLimiter` with a Redis-backed implementation (see
Document 05's `rl:{team_id}:rpm` / `rl:{team_id}:tpm` key schema and the
token_bucket.lua script) that implements this exact same `check()` method
signature, consulting the `rpm_cap` / `tpm_cap` fields already present on
TeamConfig (see app/core/config.py) — the route handler in
app/api/v1_chat.py never has to change, only the object it calls.
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
    """Always allows. No Redis, no state, no per-team accounting yet."""

    async def check(
        self, *, team_id: str, estimated_tokens: int, priority: str
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=True)

    async def reconcile(self, *, team_id: str, reserved_tokens: int, actual_tokens: int) -> None:
        """No-op in Phase 1; Phase 2 refunds the reservation delta (see TRD § Phase 2)."""
        return None
