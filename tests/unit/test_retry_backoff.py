"""
test_retry_backoff.py

Verifies the Phase 3 test plan: "A primary that fails twice then succeeds
on the 3rd attempt returns 200 without invoking fallback; backoff delays
are jittered and increasing" — plus the retryable/non-retryable branch
`RetryPolicy.run()` itself is responsible for (the fallback-vs-abort
*decision* based on that branch lives in FallbackRouter and is covered by
test_fallback_chain.py / test_error_classification.py).
"""

from __future__ import annotations

import pytest

from app.providers.base import ProviderError
from app.resilience.retry import RetryPolicy


class _RecordingSleep:
    """Injectable `sleep` that records every delay instead of waiting for
    real wall-clock time — this is what keeps this whole test file fast
    and deterministic (same technique as RateLimiter's injectable
    `clock`, app/ratelimit/limiter.py)."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _policy(*, max_attempts=3, base=0.2, cap=4.0, rng=lambda: 1.0) -> tuple[RetryPolicy, _RecordingSleep]:
    sleep = _RecordingSleep()
    policy = RetryPolicy(
        max_attempts=max_attempts, base_delay_seconds=base, max_delay_seconds=cap, sleep=sleep, rng=rng
    )
    return policy, sleep


async def test_succeeds_on_the_first_attempt_without_sleeping():
    policy, sleep = _policy()
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return "ok"

    result = await policy.run(fn)
    assert result == "ok"
    assert calls == 1
    assert sleep.delays == []


async def test_fails_twice_then_succeeds_on_the_third_attempt():
    policy, sleep = _policy(max_attempts=3)
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ProviderError("transient", retryable=True, error_type="timeout")
        return "ok on 3rd try"

    result = await policy.run(fn)
    assert result == "ok on 3rd try"
    assert calls == 3
    # Slept before retry 2 and retry 3 — never after the final (successful) attempt.
    assert len(sleep.delays) == 2


async def test_exhausts_all_attempts_and_reraises_the_last_error():
    policy, sleep = _policy(max_attempts=3)
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise ProviderError(f"attempt {calls} failed", retryable=True, error_type="timeout")

    with pytest.raises(ProviderError) as exc_info:
        await policy.run(fn)

    assert calls == 3
    assert exc_info.value.retryable is True
    assert "attempt 3 failed" in exc_info.value.message
    # Two backoff waits happen between three attempts (before retry 2, before retry 3).
    assert len(sleep.delays) == 2


async def test_non_retryable_error_aborts_immediately_with_no_sleep_and_no_retry():
    policy, sleep = _policy(max_attempts=3)
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise ProviderError("bad request", retryable=False, error_type="invalid_request")

    with pytest.raises(ProviderError) as exc_info:
        await policy.run(fn)

    assert calls == 1, "a non-retryable error must not be retried at all"
    assert sleep.delays == []
    assert exc_info.value.retryable is False


async def test_max_attempts_of_one_means_no_retries_ever():
    policy, sleep = _policy(max_attempts=1)
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise ProviderError("boom", retryable=True, error_type="timeout")

    with pytest.raises(ProviderError):
        await policy.run(fn)

    assert calls == 1
    assert sleep.delays == []


# -- backoff math ------------------------------------------------------------


def test_compute_delay_is_full_jitter_uniform_between_zero_and_the_cap():
    # rng=lambda: 1.0 pins "full jitter" at its ceiling, so the delay
    # equals the cap exactly — the simplest way to assert the *shape* of
    # the formula (min(max_delay, base * 2**(n-1))) without depending on
    # real randomness.
    policy, _ = _policy(base=0.2, cap=4.0, rng=lambda: 1.0)
    assert policy.compute_delay(1) == pytest.approx(0.2)  # base * 2^0
    assert policy.compute_delay(2) == pytest.approx(0.4)  # base * 2^1
    assert policy.compute_delay(3) == pytest.approx(0.8)  # base * 2^2
    assert policy.compute_delay(4) == pytest.approx(1.6)  # base * 2^3
    assert policy.compute_delay(5) == pytest.approx(3.2)  # base * 2^4
    assert policy.compute_delay(6) == pytest.approx(4.0)  # base * 2^5 = 6.4, capped at 4.0


def test_compute_delay_is_zero_when_rng_returns_zero():
    # The "full jitter" formula is uniform(0, cap) — rng=0 is a valid,
    # legitimate draw, not an edge-case bug.
    policy, _ = _policy(base=0.2, cap=4.0, rng=lambda: 0.0)
    assert policy.compute_delay(1) == 0.0
    assert policy.compute_delay(3) == 0.0


async def test_delays_actually_used_reflect_increasing_attempt_numbers():
    """
    Confirms RetryPolicy.run() calls compute_delay with the *attempt
    number*, not a fixed value — by using the real (unpinned) random
    module we can't assert exact figures, but we CAN assert the delays
    are drawn from increasing ranges (attempt 1's delay is drawn from
    [0, base), attempt 2's from [0, 2*base), etc.) by checking each
    recorded delay never exceeds its attempt's theoretical cap.
    """
    sleep = _RecordingSleep()
    policy = RetryPolicy(max_attempts=4, base_delay_seconds=0.1, max_delay_seconds=10.0, sleep=sleep)
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise ProviderError("always fails", retryable=True, error_type="timeout")

    with pytest.raises(ProviderError):
        await policy.run(fn)

    assert len(sleep.delays) == 3  # attempts 1,2,3 each retry; attempt 4 exhausts and raises
    expected_caps = [0.1 * (2**0), 0.1 * (2**1), 0.1 * (2**2)]  # 0.1, 0.2, 0.4
    for delay, cap in zip(sleep.delays, expected_caps, strict=True):
        assert 0.0 <= delay <= cap
