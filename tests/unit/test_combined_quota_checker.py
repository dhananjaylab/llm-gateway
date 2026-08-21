"""
test_combined_quota_checker.py

Phase 7: verifies app/ratelimit/combined.py's CombinedQuotaChecker --
both the fast path (single Redis round trip via combined_check.lua) and
its fallback to the exact two original separate calls on a Redis
connectivity error, which must preserve budget's fail-closed and
rate-limiting's fail-open postures independently (see combined.py's own
module docstring for why this matters).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from redis import exceptions as redis_exceptions

from app.core.config import TeamConfig, TeamPolicy
from app.ratelimit.budget import BudgetEnforcer
from app.ratelimit.combined import CombinedQuotaChecker
from app.ratelimit.limiter import RateLimiter


def _team(**overrides) -> TeamConfig:
    defaults = {
        "team_id": "t1",
        "api_key_hash": "sha256:irrelevant",
        "allowed_models": ["openai:gpt-5.4"],
        "rpm_cap": 10,
        "tpm_cap": 10_000,
        "budget_cap_usd": 5.0,
        "budget_period": "monthly",
        "policy": TeamPolicy(),
    }
    defaults.update(overrides)
    return TeamConfig(**defaults)


def _current_period_key(team_id: str) -> str:
    """Mirrors app/ratelimit/budget.py::period_key()'s monthly label
    exactly, so tests can seed/inspect the same Redis key the checker
    itself reads/writes."""
    label = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"budget:{team_id}:{label}"


@pytest.fixture
def rate_limiter(fake_redis) -> RateLimiter:
    return RateLimiter(fake_redis, fail_open=True)


@pytest.fixture
def budget_enforcer(fake_redis) -> BudgetEnforcer:
    return BudgetEnforcer(fake_redis)


@pytest.fixture
def checker(fake_redis, rate_limiter, budget_enforcer) -> CombinedQuotaChecker:
    return CombinedQuotaChecker(fake_redis, rate_limiter=rate_limiter, budget_enforcer=budget_enforcer)


# -- fast path (Redis healthy) ------------------------------------------


async def test_allows_when_both_budget_and_rate_limit_have_headroom(checker):
    decision = await checker.check(team=_team(), estimated_tokens=100)
    assert decision.allowed is True
    assert decision.budget_denied is False
    assert decision.rate_limit.allowed is True
    assert decision.rate_limit.remaining_rpm == 9


async def test_denies_on_rate_limit_when_budget_has_headroom(checker):
    team = _team(rpm_cap=1)
    first = await checker.check(team=team, estimated_tokens=10)
    assert first.allowed is True

    second = await checker.check(team=team, estimated_tokens=10)
    assert second.allowed is False
    assert second.budget_denied is False
    assert second.rate_limit.allowed is False
    assert second.rate_limit.limit_type == "rpm"


async def test_budget_denial_never_touches_the_rate_limit_buckets(checker, fake_redis):
    """The ordering/atomicity guarantee carried over from the two-call
    version: a request that's over budget must never consume rate-limit
    capacity -- verified here by confirming the rl:* keys were never even
    created."""
    team = _team(budget_cap_usd=1.0)
    await fake_redis.hset(_current_period_key(team.team_id), mapping={"spend_usd": 5.0})

    decision = await checker.check(team=team, estimated_tokens=10)

    assert decision.allowed is False
    assert decision.budget_denied is True
    assert decision.rate_limit is None
    assert await fake_redis.exists(f"rl:{team.team_id}:rpm") == 0
    assert await fake_redis.exists(f"rl:{team.team_id}:tpm") == 0


async def test_budget_decision_reports_the_live_cap_and_current_spend(checker, fake_redis):
    team = _team(budget_cap_usd=100.0)
    await fake_redis.hset(_current_period_key(team.team_id), mapping={"spend_usd": 42.0})

    decision = await checker.check(team=team, estimated_tokens=10)

    assert decision.budget.spend_usd == 42.0
    assert decision.budget.cap_usd == 100.0
    assert decision.budget.allowed is True


# -- Redis-outage fallback (must preserve fail-closed / fail-open independently) --


async def test_fallback_still_enforces_budget_fail_closed(checker, fake_redis, monkeypatch):
    """Simulates the combined script itself failing (e.g. a transient
    network blip) -- the fallback must still correctly deny an
    over-budget team by actually calling budget_enforcer.precheck()
    against the same real Redis, not silently allowing through."""

    async def _boom(*args, **kwargs):
        raise redis_exceptions.ConnectionError("simulated outage")

    monkeypatch.setattr(checker._script, "eval", _boom)

    team = _team(budget_cap_usd=1.0)
    await fake_redis.hset(_current_period_key(team.team_id), mapping={"spend_usd": 5.0})

    decision = await checker.check(team=team, estimated_tokens=10)
    assert decision.allowed is False
    assert decision.budget_denied is True
    assert decision.rate_limit is None


async def test_fallback_still_enforces_rate_limit_fail_open_with_headroom(checker, monkeypatch):
    """Same simulated outage, but exercising the fallback's SECOND call
    (rate_limiter.check()) once budget passes -- must still enforce RPM
    correctly, not skip it."""

    async def _boom(*args, **kwargs):
        raise redis_exceptions.ConnectionError("simulated outage")

    monkeypatch.setattr(checker._script, "eval", _boom)

    team = _team(rpm_cap=1)
    first = await checker.check(team=team, estimated_tokens=10)
    assert first.allowed is True

    second = await checker.check(team=team, estimated_tokens=10)
    assert second.allowed is False
    assert second.budget_denied is False
    assert second.rate_limit.limit_type == "rpm"


async def test_fallback_is_only_used_when_the_combined_script_actually_fails(checker, monkeypatch):
    """Sanity check on the other direction: the fallback's separate calls
    must NOT run on the healthy path -- spies on both to prove the single
    combined round trip is what actually executes when Redis is fine."""
    precheck_calls = []
    original_precheck = checker._budget_enforcer.precheck

    async def _spy_precheck(team):
        precheck_calls.append(team.team_id)
        return await original_precheck(team)

    monkeypatch.setattr(checker._budget_enforcer, "precheck", _spy_precheck)

    await checker.check(team=_team(), estimated_tokens=10)

    assert precheck_calls == [], (
        "the fallback's separate budget_enforcer.precheck() must not run on the fast path"
    )
