"""
app/ratelimit/combined.py

Phase 7 optimization (Doc 1's own bottleneck table: "Sequential Redis
calls for RPM, TPM, and Budget evaluation... Network RTT accumulation on
hot request paths"). Evaluates the budget precheck and the RPM/TPM token
bucket in ONE atomic Redis round trip (combined_check.lua) instead of two
sequential calls (BudgetEnforcer.precheck() then RateLimiter.check()).

SAFETY DESIGN — read this before changing anything here: combining the
two checks into one Redis call means a Redis outage can no longer fail
each domain independently the way the separate calls do (Document 06's
own Appendix A: budget fails CLOSED, "safer for budget enforcement";
rate limiting fails OPEN by default, "safer for availability" — these are
DIFFERENT postures for DIFFERENT reasons, both already tested by
test_budget_enforcement.py and test_token_bucket_concurrency.py
respectively). `check()` below is therefore a FAST PATH ONLY: on any
Redis connectivity error from the combined script, it falls back to
running the exact two ORIGINAL separate calls
(`budget_enforcer.precheck()` then `rate_limiter.check()`), preserving
both existing, already-tested failure postures byte-for-byte. The
combined script only ever changes observed behavior on the Redis-healthy
happy path (the overwhelming majority of traffic, and where the latency
actually matters) — it can never change what happens when Redis is down.

SCOPE: only used for the normal/high-priority hot path in
app/api/v1_chat.py. Batch-priority requests keep their own separate
budget precheck + queueing loop (app/ratelimit/priority_queue.py) — that
path already retries the rate-limit check itself over up to
BATCH_QUEUE_MAX_WAIT_SECONDS, which a single combined round trip isn't
shaped for, and combining them would mean re-deriving the budget-cap
check inside a retry loop for no latency benefit (the loop is already
optimizing for "wait for capacity," not "minimize round trips").
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
from app.ratelimit.budget import BudgetDecision, BudgetEnforcer, period_key
from app.ratelimit.limiter import RateLimitDecision, RateLimiter

logger = logging.getLogger("gateway.combined_quota")

_SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass
class CombinedDecision:
    allowed: bool
    budget_denied: bool
    budget: BudgetDecision
    # None only when budget_denied is True — the rate-limit buckets were
    # never evaluated, matching the pre-Phase-7 behavior where
    # RateLimiter.check() was never even called on a budget rejection.
    rate_limit: RateLimitDecision | None


class CombinedQuotaChecker:
    def __init__(
        self,
        redis: Redis,
        *,
        rate_limiter: RateLimiter,
        budget_enforcer: BudgetEnforcer,
        rl_key_ttl_seconds: int = 7200,
        clock=time.time,
    ) -> None:
        self._redis = redis
        self._rate_limiter = rate_limiter
        self._budget_enforcer = budget_enforcer
        self._ttl = rl_key_ttl_seconds
        self._clock = clock
        script_text = (_SCRIPT_DIR / "combined_check.lua").read_text(encoding="utf-8")
        self._script = LuaScript(redis, script_text, name="combined_check")

    async def check(self, *, team: TeamConfig, estimated_tokens: int) -> CombinedDecision:
        """
        May raise `BudgetUnavailableError` (app/ratelimit/budget.py) —
        both on the fast path (a genuinely unreachable Redis surfaces
        through the fallback below, which calls
        `budget_enforcer.precheck()` directly) and via that same fallback
        call. This is intentional and unchanged from pre-Phase-7 behavior:
        budget fails closed, the caller (app/api/v1_chat.py) already
        catches this and returns 503.
        """
        key, _ = period_key(team.team_id, team.budget_period)
        rpm_refill = team.rpm_cap / 60.0
        tpm_refill = team.tpm_cap / 60.0

        try:
            raw = await self._script.eval(
                keys=[f"rl:{team.team_id}:rpm", f"rl:{team.team_id}:tpm", key],
                args=[
                    self._clock(),
                    team.rpm_cap,
                    rpm_refill,
                    team.tpm_cap,
                    tpm_refill,
                    1,  # requested_rpm
                    estimated_tokens,  # requested_tpm
                    self._ttl,
                    team.budget_cap_usd,
                ],
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            logger.warning(
                "redis unavailable during combined quota check for team=%s — falling back to "
                "the two separate calls (preserves budget fail-closed / rate-limit fail-open "
                "independently — see this module's own docstring)",
                team.team_id,
                exc_info=True,
            )
            return await self._separate_calls_fallback(team=team, estimated_tokens=estimated_tokens)

        allowed, budget_denied, remaining_rpm, remaining_tpm, retry_after, spend_usd = raw
        budget_denied = bool(int(budget_denied))
        allowed = bool(int(allowed))

        budget_decision = BudgetDecision(
            allowed=not budget_denied,
            spend_usd=float(spend_usd),
            cap_usd=team.budget_cap_usd,
            period=key.rsplit(":", 1)[-1],
        )

        if budget_denied:
            return CombinedDecision(
                allowed=False, budget_denied=True, budget=budget_decision, rate_limit=None
            )

        limit_type = None
        if not allowed:
            limit_type = "rpm" if int(remaining_rpm) < 1 else "tpm"
        rate_decision = RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=int(retry_after) or None,
            remaining_rpm=int(remaining_rpm),
            remaining_tpm=int(remaining_tpm),
            limit_type=limit_type,
        )
        return CombinedDecision(
            allowed=allowed, budget_denied=False, budget=budget_decision, rate_limit=rate_decision
        )

    async def _separate_calls_fallback(self, *, team: TeamConfig, estimated_tokens: int) -> CombinedDecision:
        """
        Exact pre-Phase-7 call sequence: budget precheck (fail-closed —
        raises BudgetUnavailableError on its own if Redis is genuinely
        down; not caught here, propagates to the caller exactly as it
        always has) then, only if budget allows, the rate limiter's own
        check() (fail-open with its local fallback bucket — also
        unchanged).
        """
        budget_decision = await self._budget_enforcer.precheck(team)
        if not budget_decision.allowed:
            return CombinedDecision(
                allowed=False, budget_denied=True, budget=budget_decision, rate_limit=None
            )

        rate_decision = await self._rate_limiter.check(
            team=team, estimated_tokens=estimated_tokens, priority="normal"
        )
        return CombinedDecision(
            allowed=rate_decision.allowed,
            budget_denied=False,
            budget=budget_decision,
            rate_limit=rate_decision,
        )
