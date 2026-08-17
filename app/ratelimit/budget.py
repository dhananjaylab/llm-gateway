"""
Budget enforcement (TRD, "Budget Enforcement Formulas and Prompt-Cache
Accounting"; Document 03 Journey C).

Ordering decision (resolves the TRD's own "evaluate both before consuming
either, or roll back the rate-limit decrement on a budget rejection"
instruction): `precheck()` is a read-only comparison of current spend
against the cap and runs BEFORE `RateLimiter.check()` reserves any
capacity. Since precheck never mutates state, there is nothing to roll
back on a budget rejection — a request that fails precheck never touches
the rate-limit bucket at all. This is simpler than the alternative (do
both, roll back the rate-limit decrement if budget fails after) and
produces the same guarantee: no request ever partially consumes shared
state it isn't going to be allowed to use.

The actual dollar cost of a request is unknowable before the call
completes (output token count is the provider's decision), so `precheck`
can only ever ask "has this team ALREADY exceeded its cap" — the request
that pushes spend from just-under to over the cap is still billed (it
already happened by the time cost is known); the *next* request is what
gets blocked. This is the standard shape of a post-paid metered spend cap
and matches the PRD's own wording ("When they hit it, block requests").

Fail-closed (TRD Appendix A, resolved, not a toggle): if Redis is
unreachable, `precheck()` raises rather than silently allowing spend to
go unmetered — "safer for budget enforcement" per the TRD's own stated
reasoning. Contrast with RateLimiter, which is fail-OPEN by default.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from app.core.config import TeamConfig
from app.core.redis_script import LuaScript
from app.observability.metrics import GatewayMetrics

logger = logging.getLogger("gateway.budget")

_SCRIPT_DIR = Path(__file__).resolve().parent


class BudgetUnavailableError(Exception):
    """Raised when Redis is unreachable during a budget precheck (fail-closed)."""


@dataclass
class BudgetDecision:
    allowed: bool
    spend_usd: float
    cap_usd: float
    period: str
    warning_fraction: float | None = None  # set only on the call that just crossed 80%


def period_key(team_id: str, budget_period: str, *, now: datetime | None = None) -> tuple[str, int]:
    """Returns (redis_key, ttl_seconds_until_period_rollover)."""
    now = now or datetime.now(timezone.utc)
    if budget_period == "daily":
        period_label = now.strftime("%Y-%m-%d")
        seconds_in_period = 86400
        elapsed = now.hour * 3600 + now.minute * 60 + now.second
    else:  # "monthly" (default) — anything else falls back to monthly semantics
        period_label = now.strftime("%Y-%m")
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        seconds_in_period = days_in_month * 86400
        elapsed = (now.day - 1) * 86400 + now.hour * 3600 + now.minute * 60 + now.second

    ttl = max(60, seconds_in_period - elapsed + 3600)  # +1h buffer past rollover
    return f"budget:{team_id}:{period_label}", ttl


class BudgetEnforcer:
    def __init__(
        self, redis: Redis, *, warn_fraction: float = 0.8, metrics: GatewayMetrics | None = None
    ) -> None:
        self._redis = redis
        self._warn_fraction = warn_fraction
        self._metrics = metrics
        script_text = (_SCRIPT_DIR / "budget_increment.lua").read_text(encoding="utf-8")
        self._script = LuaScript(redis, script_text, name="budget_increment")

    async def precheck(self, team: TeamConfig) -> BudgetDecision:
        """
        BUGFIX (Phase 5): previously read `cap_usd` back from the
        per-period Redis hash and preferred it over `team.budget_cap_usd`
        whenever it was present -- but that stored value is whatever was
        active the first time `record_spend()` ran this period (see
        budget_increment.lua's own bugfix note), so an admin's PATCH to
        the live cap had no enforcement effect for the rest of an
        already-active period. `team.budget_cap_usd` (resolved fresh by
        the caller from TeamConfigStore on every request, same hot-reload
        path every other admin-changeable setting in this codebase uses)
        is now always the enforcement cap; only `spend_usd` is read from
        the ledger.
        """
        key, _ = period_key(team.team_id, team.budget_period)
        try:
            raw = await self._redis.hmget(key, "spend_usd")
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError) as exc:
            raise BudgetUnavailableError(
                f"redis unavailable during budget precheck for team={team.team_id}"
            ) from exc

        spend = float(raw[0]) if raw[0] is not None else 0.0
        cap = team.budget_cap_usd

        return BudgetDecision(
            allowed=spend < cap,
            spend_usd=spend,
            cap_usd=cap,
            period=key.rsplit(":", 1)[-1],
        )

    async def record_spend(self, team: TeamConfig, cost_usd: float) -> BudgetDecision:
        """
        Called AFTER a successful provider call with the real, computed
        cost. Best-effort against a Redis outage: a request that already
        succeeded and was returned to the client should not fail (or
        block) because the ledger write couldn't land — the loss is
        logged loudly and the next precheck simply undercounts slightly
        until Redis recovers, which is the same "log loudly either way"
        posture the TRD asks for on the rate-limit side.
        """
        key, ttl = period_key(team.team_id, team.budget_period)
        try:
            raw = await self._script.eval(
                keys=[key],
                args=[cost_usd, team.budget_cap_usd, self._warn_fraction, ttl],
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            logger.error(
                "redis unavailable while recording spend for team=%s cost_usd=%.6f — "
                "spend NOT persisted, budget ledger will undercount until Redis recovers",
                team.team_id,
                cost_usd,
                exc_info=True,
            )
            return BudgetDecision(
                allowed=True, spend_usd=0.0, cap_usd=team.budget_cap_usd, period=key
            )

        new_spend, cap, crossed_warning = raw
        crossed_warning = bool(int(crossed_warning))

        if self._metrics is not None and float(cap):
            self._metrics.budget_utilization_ratio.labels(team_id=team.team_id).set(
                float(new_spend) / float(cap)
            )

        if crossed_warning:
            logger.warning(
                "budget warning: team=%s spend_usd=%s cap_usd=%s (%.0f%% of cap) — "
                "Phase 4 wires this event to a Slack webhook; logged here for now",
                team.team_id,
                new_spend,
                cap,
                (float(new_spend) / float(cap)) * 100 if float(cap) else 0.0,
            )

        return BudgetDecision(
            allowed=True,
            spend_usd=float(new_spend),
            cap_usd=float(cap),
            period=key.rsplit(":", 1)[-1],
            warning_fraction=(float(new_spend) / float(cap)) if crossed_warning and float(cap) else None,
        )
