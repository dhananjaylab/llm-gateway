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

Phase 8: `check`/`peek`/`reconcile` (team-level) are now thin wrappers
around a generic `_check_bucket`/`_peek_bucket`/`_reconcile_bucket` core,
reused verbatim by the new `check_org`/`peek_org`/`reconcile_org`
(docs/PHASE8_KICKOFF_SCOPING.md §6, Option A) — org-level buckets are
keyed `rl:org:{org_id}:{rpm,tpm}` (a distinct namespace from team's
`rl:{team_id}:...`, so an org_id can never collide with a team_id even
if the two strings happen to match), reuse token_bucket.lua UNCHANGED
(it only ever cared about two generic HASH keys, never "team" by name),
and get their own local-fallback bucket keyed by a generic string id
rather than `team.team_id`. This keeps the already-tested team-level
path's exact behavior (same script, same call shape) while adding org
enforcement as a genuinely additive layer, not a rewrite.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from app.core.config import OrgConfig, TeamConfig
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

    # -- public API: team-level (Phase 2) -------------------------------------

    async def check(
        self, *, team: TeamConfig, estimated_tokens: int, priority: str
    ) -> RateLimitDecision:
        return await self._check_bucket(
            rpm_key=f"rl:{team.team_id}:rpm",
            tpm_key=f"rl:{team.team_id}:tpm",
            rpm_cap=team.rpm_cap,
            tpm_cap=team.tpm_cap,
            estimated_tokens=estimated_tokens,
            local_bucket_id=f"team:{team.team_id}",
            log_label=f"team={team.team_id}",
        )

    async def peek(self, team: TeamConfig) -> RateLimitDecision:
        """Read-only: report current remaining capacity without consuming any."""
        return await self._peek_bucket(
            rpm_key=f"rl:{team.team_id}:rpm",
            tpm_key=f"rl:{team.team_id}:tpm",
            rpm_cap=team.rpm_cap,
            tpm_cap=team.tpm_cap,
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
        await self._reconcile_bucket(
            tpm_key=f"rl:{team.team_id}:tpm",
            tpm_cap=team.tpm_cap,
            reserved_tokens=reserved_tokens,
            actual_tokens=actual_tokens,
            log_label=f"team={team.team_id}",
        )

    # -- public API: org-level (Phase 8, Option A) ----------------------------

    async def check_org(self, org: OrgConfig, estimated_tokens: int) -> RateLimitDecision:
        return await self._check_bucket(
            rpm_key=f"rl:org:{org.org_id}:rpm",
            tpm_key=f"rl:org:{org.org_id}:tpm",
            rpm_cap=org.rpm_cap,
            tpm_cap=org.tpm_cap,
            estimated_tokens=estimated_tokens,
            local_bucket_id=f"org:{org.org_id}",
            log_label=f"org={org.org_id}",
        )

    async def peek_org(self, org: OrgConfig) -> RateLimitDecision:
        return await self._peek_bucket(
            rpm_key=f"rl:org:{org.org_id}:rpm",
            tpm_key=f"rl:org:{org.org_id}:tpm",
            rpm_cap=org.rpm_cap,
            tpm_cap=org.tpm_cap,
        )

    async def reconcile_org(self, *, org: OrgConfig, reserved_tokens: int, actual_tokens: int) -> None:
        await self._reconcile_bucket(
            tpm_key=f"rl:org:{org.org_id}:tpm",
            tpm_cap=org.tpm_cap,
            reserved_tokens=reserved_tokens,
            actual_tokens=actual_tokens,
            log_label=f"org={org.org_id}",
        )

    # -- generic core, shared by team and org call sites ----------------------

    async def _check_bucket(
        self,
        *,
        rpm_key: str,
        tpm_key: str,
        rpm_cap: int,
        tpm_cap: int,
        estimated_tokens: int,
        local_bucket_id: str,
        log_label: str,
    ) -> RateLimitDecision:
        now = self._clock()
        rpm_refill = rpm_cap / 60.0
        tpm_refill = tpm_cap / 60.0

        try:
            raw = await self._script.eval(
                keys=[rpm_key, tpm_key],
                args=[now, rpm_cap, rpm_refill, tpm_cap, tpm_refill, 1, estimated_tokens, self._ttl],
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError) as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "redis unavailable during rate-limit check for %s — "
                "falling back to local, non-distributed limiter (RATE_LIMIT_FAIL_OPEN=true)",
                log_label,
                exc_info=exc,
            )
            return self._check_local(local_bucket_id, rpm_cap, tpm_cap, estimated_tokens)

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

    async def _peek_bucket(
        self, *, rpm_key: str, tpm_key: str, rpm_cap: int, tpm_cap: int
    ) -> RateLimitDecision:
        now = self._clock()
        raw = await self._script.eval(
            keys=[rpm_key, tpm_key],
            args=[now, rpm_cap, rpm_cap / 60.0, tpm_cap, tpm_cap / 60.0, 0, 0, self._ttl],
        )
        _, remaining_rpm, remaining_tpm, _ = raw
        return RateLimitDecision(
            allowed=True, remaining_rpm=int(remaining_rpm), remaining_tpm=int(remaining_tpm)
        )

    async def _reconcile_bucket(
        self, *, tpm_key: str, tpm_cap: int, reserved_tokens: int, actual_tokens: int, log_label: str
    ) -> None:
        refund = reserved_tokens - actual_tokens
        if refund <= 0:
            return
        try:
            await self._reconcile_script.eval(keys=[tpm_key], args=[refund, tpm_cap, self._ttl])
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            # Reconciliation failing is not worth failing the (already
            # successful) request over — the bucket stays slightly smaller
            # than it should until the key's TTL naturally expires it.
            logger.warning(
                "redis unavailable during reconciliation for %s — refund skipped",
                log_label,
                exc_info=True,
            )

    # -- local (non-distributed) fallback path --------------------------------

    def _check_local(
        self, bucket_id: str, rpm_cap: int, tpm_cap: int, estimated_tokens: int
    ) -> RateLimitDecision:
        rpm_bucket, tpm_bucket = self._local_buckets.get(bucket_id, (None, None))
        if rpm_bucket is None:
            rpm_bucket = _LocalBucket(rpm_cap)
            tpm_bucket = _LocalBucket(tpm_cap)
            self._local_buckets[bucket_id] = (rpm_bucket, tpm_bucket)

        now = time.monotonic()
        for bucket, capacity, per_minute in (
            (rpm_bucket, rpm_cap, rpm_cap),
            (tpm_bucket, tpm_cap, tpm_cap),
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
