"""
test_circuit_breaker_error_rate.py

Phase 7: verifies CIRCUIT_BREAKER_MODE=error_rate — the time-windowed
sibling of the default fixed_count mode (see
circuit_record_error_rate.lua's own header and TRD Appendix A's own
anticipated v2 extension: "move to a rolling error-rate percentage only
if the fixed threshold proves too twitchy").

Same unit-level-against-CircuitBreaker-directly approach as
test_circuit_breaker.py, for the same reason: precise control over
"exactly how many seconds have elapsed" is what proves the time-window
trimming behavior (the one thing that's actually different from
fixed_count mode — Closed/Open/Half-Open transition mechanics themselves
are unchanged, since circuit_check.lua is reused unmodified for both
modes; test_circuit_breaker.py's existing Half-Open/probe tests already
cover that shared machinery and aren't duplicated here).
"""

from __future__ import annotations

import pytest

from app.resilience.circuit_breaker import CircuitBreaker


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def breaker(fake_redis, clock: _FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        fake_redis,
        mode="error_rate",
        error_rate_window_seconds=30.0,
        error_rate_threshold=0.5,
        error_rate_minimum_samples=4,
        cooldown_seconds=10.0,
        clock=clock,
    )


async def test_stays_closed_below_minimum_samples_even_at_100_percent_failure_rate(
    breaker: CircuitBreaker,
):
    """The floor that keeps a percentage threshold from being twitchier
    than fixed_count, not less -- 3 failures / 3 total = 100% error rate,
    but minimum_samples=4 means it must not trip yet."""
    for _ in range(3):
        await breaker.record_failure(provider="openai", model="gpt-5.4")

    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "closed"
    assert status.failures_in_window == 3


async def test_opens_once_minimum_samples_reached_and_error_rate_at_or_above_threshold(
    breaker: CircuitBreaker,
):
    """4 samples (minimum_samples), 2 failures = 50% (>= threshold 0.5)."""
    await breaker.record_success(provider="openai", model="gpt-5.4")
    await breaker.record_failure(provider="openai", model="gpt-5.4")
    await breaker.record_success(provider="openai", model="gpt-5.4")
    await breaker.record_failure(provider="openai", model="gpt-5.4")

    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is False
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "open"


async def test_stays_closed_when_error_rate_is_below_threshold_regardless_of_sample_count(
    breaker: CircuitBreaker,
):
    """10 samples, 3 failures = 30% (< threshold 0.5) -- well above
    minimum_samples, must still stay closed."""
    outcomes = [False] * 7 + [True] * 3  # False=success, True=failure
    for is_failure in outcomes:
        if is_failure:
            await breaker.record_failure(provider="openai", model="gpt-5.4")
        else:
            await breaker.record_success(provider="openai", model="gpt-5.4")

    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "closed"
    assert status.failures_in_window == 3


async def test_failures_older_than_the_window_age_out_and_stop_counting(
    breaker: CircuitBreaker, clock: _FakeClock
):
    """The actual behavioral difference from fixed_count mode: a failure
    from before the window started must not count toward the current
    error rate, no matter how many total calls have happened since."""
    for _ in range(4):
        await breaker.record_failure(provider="openai", model="gpt-5.4")
    # 4/4 = 100% -- would trip immediately (minimum_samples=4 already met)
    status_before = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status_before.state == "open"

    # A brand new provider/model pair (fresh window) shouldn't inherit
    # this -- sanity-checking isolation before the real time-aging test.
    other_status = await breaker.get_status(provider="anthropic", model="claude-sonnet-5")
    assert other_status.state == "closed"

    # Advance well past the 30s window and record fresh successes only --
    # the 4 old failures must have aged out, so the circuit can recover
    # to Closed on evaluation (half_open handling aside; this provider is
    # already Open, so record_success while Open doesn't re-evaluate the
    # window until a probe happens -- test the aging directly via a
    # SEPARATE pair that never opened, to isolate window-trimming from
    # the Open/Half-Open state machine, which test_circuit_breaker.py
    # already covers).
    clock.advance(5.0)
    for _ in range(3):
        await breaker.record_failure(provider="gemini", model="gemini-3.6-flash")
    # 3/3 = 100% but below minimum_samples(4) -- stays closed.
    assert (await breaker.get_status(provider="gemini", model="gemini-3.6-flash")).state == "closed"

    clock.advance(31.0)  # past window_seconds=30 relative to those 3 failures
    await breaker.record_success(provider="gemini", model="gemini-3.6-flash")
    status = await breaker.get_status(provider="gemini", model="gemini-3.6-flash")
    # The 3 old failures aged out of the window; only the 1 fresh success
    # remains -- 0 failures / 1 total, well under minimum_samples anyway.
    assert status.failures_in_window == 0
    assert status.state == "closed"


async def test_zset_members_are_unique_per_call_not_deduped_by_redis(
    breaker: CircuitBreaker, clock: _FakeClock
):
    """Regression guard for the exact bug circuit_record_error_rate.lua's
    own header warns about: ZSET members must be unique (uuid4-suffixed)
    or Redis silently collapses identical values and the window
    undercounts. Records more failures than minimum_samples at the exact
    same clock timestamp (a real burst would land in the same second)."""
    clock_frozen_at = clock.now
    for _ in range(5):
        await breaker.record_failure(provider="openai", model="gpt-5.4")
        assert clock.now == clock_frozen_at  # confirms these really are same-timestamp calls

    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.failures_in_window == 5, (
        "all 5 failures recorded at the identical timestamp must still be counted individually -- "
        "a dedup bug here would silently undercount error rate"
    )


async def test_half_open_probe_mechanics_are_unchanged_from_fixed_count_mode(
    breaker: CircuitBreaker, clock: _FakeClock
):
    """circuit_check.lua is reused UNCHANGED for both modes (see
    circuit_record_error_rate.lua's own header) -- one confirming test
    that error_rate mode still gets a real Open -> Half-Open -> Closed
    recovery, not a full re-test of test_circuit_breaker.py's dedicated
    Half-Open coverage."""
    for _ in range(4):
        await breaker.record_failure(provider="openai", model="gpt-5.4")
    assert (await breaker.get_status(provider="openai", model="gpt-5.4")).state == "open"

    clock.advance(10.1)  # past cooldown_seconds=10
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True  # the probe

    await breaker.record_success(provider="openai", model="gpt-5.4")
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "closed"
    assert status.failures_in_window == 0, (
        "the ZSET window must be cleared on a successful probe, same as the LIST in fixed_count mode"
    )


async def test_redis_outage_fails_open_same_as_fixed_count_mode(breaker: CircuitBreaker, monkeypatch):
    from redis import exceptions as redis_exceptions

    async def _boom(*_args, **_kwargs):
        raise redis_exceptions.ConnectionError("simulated redis outage")

    monkeypatch.setattr(breaker._check_script, "eval", _boom)
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True


async def test_fixed_count_mode_is_still_the_default_when_mode_is_not_specified(fake_redis):
    """The zero-behavior-change guarantee: constructing a CircuitBreaker
    without passing `mode` must still use circuit_record.lua, not the
    error_rate script -- every Phase 1-6 test that builds one directly
    depends on this."""
    breaker = CircuitBreaker(fake_redis)
    assert breaker._mode == "fixed_count"
    assert breaker._record_script._name == "circuit_record"
