"""
Phase 2 rate limiter — replaces app/ratelimit/stub.py's permissive no-op.

Implements the same `RateLimitDecision` / `check()` / `reconcile()`
surface the stub already established in Phase 1 (the TRD's whole point in
requiring that stub: "Phase 2 is a drop-in replacement, not a refactor")
plus the two-phase reservation protocol on top of it.

Redis-down handling resolves Document 06 Appendix A's open decision
("Redis failure mode... Recommended: fail-open with a conservative local
limiter for rate limiting"): if Redis is unreachable, `check()` logs
loudly and falls back to a small in-process token bucket per team,
seeded from the same `rpm_cap`/`tpm_cap` the team already has — it is
*not* distributed (each gateway instance would enforce its own local
copy of the limit until Redis comes back), which is exactly the
documented trade-off of "conservative... safer for availability."
Toggle via `RATE_LIMIT_FAIL_OPEN=false` to fail-closed instead (reject
with 503 rather than degrade).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from app.core.config import TeamConfig
from app.core.redis_script import LuaScript

logger = logging.getLogger("gateway.ratelimit")

_SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int | None = None
    remaining_rpm: int | None = None
    remaining_tpm: int | None = None
    limit_type: str | None = None  # "rpm" | "tpm" | None (which axis denied, if any)


class _LocalBucket:
    """Non-distributed fallback bucket, used only while Redis is unreachable."""

    __slots__ = ("last_update", "tokens")

    def __init__(self, capacity: float) -> None:
        self.tokens = capacity
        self.last_update = time.monotonic()


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        key_ttl_seconds: int = 7200,
        fail_open: bool = True,
        clock=time.time,
    ) -> None:
        self._redis = redis
        self._ttl = key_ttl_seconds
        self._fail_open = fail_open
        self._clock = clock

        script_text = (_SCRIPT_DIR / "token_bucket.lua").read_text(encoding="utf-8")
        self._script = LuaScript(redis, script_text, name="token_bucket")

        reconcile_text = (_SCRIPT_DIR / "reconcile_tpm.lua").read_text(encoding="utf-8")
        self._reconcile_script = LuaScript(redis, reconcile_text, name="reconcile_tpm")

        self._local_buckets: dict[str, tuple[_LocalBucket, _LocalBucket]] = {}

    # -- public API ----------------------------------------------------------

    async def check(
        self, *, team: TeamConfig, estimated_tokens: int, priority: str
    ) -> RateLimitDecision:
        now = self._clock()
        rpm_refill = team.rpm_cap / 60.0
        tpm_refill = team.tpm_cap / 60.0

        try:
            raw = await self._script.eval(
                keys=[f"rl:{team.team_id}:rpm", f"rl:{team.team_id}:tpm"],
                args=[
                    now,
                    team.rpm_cap,
                    rpm_refill,
                    team.tpm_cap,
                    tpm_refill,
                    1,  # requested_rpm
                    estimated_tokens,  # requested_tpm
                    self._ttl,
                ],
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError) as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "redis unavailable during rate-limit check for team=%s — "
                "falling back to local, non-distributed limiter (RATE_LIMIT_FAIL_OPEN=true)",
                team.team_id,
                exc_info=exc,
            )
            return self._check_local(team, estimated_tokens)

        allowed, remaining_rpm, remaining_tpm, retry_after = raw
        allowed = bool(int(allowed))
        limit_type = None
        if not allowed:
            limit_type = "rpm" if int(remaining_rpm) < 1 else "tpm"

        return RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=int(retry_after) or None,
            remaining_rpm=int(remaining_rpm),
            remaining_tpm=int(remaining_tpm),
            limit_type=limit_type,
        )

    async def peek(self, team: TeamConfig) -> RateLimitDecision:
        """Read-only: report current remaining capacity without consuming any."""
        now = self._clock()
        raw = await self._script.eval(
            keys=[f"rl:{team.team_id}:rpm", f"rl:{team.team_id}:tpm"],
            args=[
                now,
                team.rpm_cap,
                team.rpm_cap / 60.0,
                team.tpm_cap,
                team.tpm_cap / 60.0,
                0,
                0,
                self._ttl,
            ],
        )
        _, remaining_rpm, remaining_tpm, _ = raw
        return RateLimitDecision(
            allowed=True, remaining_rpm=int(remaining_rpm), remaining_tpm=int(remaining_tpm)
        )

    async def reconcile(self, *, team: TeamConfig, reserved_tokens: int, actual_tokens: int) -> None:
        """
        Refund the unused portion of a TPM reservation now that real usage
        is known. A no-op if actual usage met or exceeded the reservation
        (see reconcile_tpm.lua's docstring — overage is accepted, not
        clawed back). Never touches RPM: RPM reserves exactly 1 per
        request and that request did happen, so there is nothing to
        refund on that axis.
        """
        refund = reserved_tokens - actual_tokens
        if refund <= 0:
            return
        try:
            await self._reconcile_script.eval(
                keys=[f"rl:{team.team_id}:tpm"],
                args=[refund, team.tpm_cap, self._ttl],
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            # Reconciliation failing is not worth failing the (already
            # successful) request over — the team keeps a slightly smaller
            # bucket than it should until the key's TTL naturally expires it.
            logger.warning(
                "redis unavailable during reconciliation for team=%s — refund skipped",
                team.team_id,
                exc_info=True,
            )

    # -- local (non-distributed) fallback path --------------------------------

    def _check_local(self, team: TeamConfig, estimated_tokens: int) -> RateLimitDecision:
        rpm_bucket, tpm_bucket = self._local_buckets.get(team.team_id, (None, None))
        if rpm_bucket is None:
            rpm_bucket = _LocalBucket(team.rpm_cap)
            tpm_bucket = _LocalBucket(team.tpm_cap)
            self._local_buckets[team.team_id] = (rpm_bucket, tpm_bucket)

        now = time.monotonic()
        for bucket, capacity, per_minute in (
            (rpm_bucket, team.rpm_cap, team.rpm_cap),
            (tpm_bucket, team.tpm_cap, team.tpm_cap),
        ):
            delta = max(0.0, now - bucket.last_update)
            bucket.tokens = min(capacity, bucket.tokens + delta * (per_minute / 60.0))
            bucket.last_update = now

        if rpm_bucket.tokens >= 1 and tpm_bucket.tokens >= estimated_tokens:
            rpm_bucket.tokens -= 1
            tpm_bucket.tokens -= estimated_tokens
            return RateLimitDecision(
                allowed=True,
                remaining_rpm=int(rpm_bucket.tokens),
                remaining_tpm=int(tpm_bucket.tokens),
            )

        limit_type = "rpm" if rpm_bucket.tokens < 1 else "tpm"
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=1,
            remaining_rpm=int(rpm_bucket.tokens),
            remaining_tpm=int(tpm_bucket.tokens),
            limit_type=limit_type,
        )
