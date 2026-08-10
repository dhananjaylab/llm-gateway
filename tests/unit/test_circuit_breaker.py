"""
test_circuit_breaker.py

Verifies the Phase 3 test plan's
`test_circuit_breaker_transitions.py` line: "Forced consecutive failures
trip Closed -> Open; requests during Open skip the network call entirely
(assert no outbound call made); after the cooldown, exactly one probe is
sent in Half-Open; probe success -> Closed, probe failure -> back to Open."

These are unit-level tests against `CircuitBreaker` directly (fakeredis,
injectable clock) rather than through the HTTP API — precise control over
"exactly how many seconds have elapsed" is what proves the Half-Open
single-probe invariant, and that's much easier to assert deterministically
here than by making an HTTP test suite actually wait out a cooldown.
End-to-end confirmation that the *wired-up* circuit breaker (real Redis,
real clock) actually gates traffic through the full request pipeline
lives in test_fallback_chain.py.
"""

from __future__ import annotations

import pytest
from redis import exceptions as redis_exceptions

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
        failure_threshold=3,
        window_size=5,
        cooldown_seconds=10.0,
        clock=clock,
    )


async def test_allows_by_default_for_a_never_seen_provider_model(breaker: CircuitBreaker):
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "closed"


async def test_stays_closed_below_the_failure_threshold(breaker: CircuitBreaker):
    await breaker.record_failure(provider="openai", model="gpt-5.4")
    await breaker.record_failure(provider="openai", model="gpt-5.4")
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True


async def test_opens_once_failures_in_the_window_reach_the_threshold(breaker: CircuitBreaker):
    for _ in range(3):  # threshold=3
        await breaker.record_failure(provider="openai", model="gpt-5.4")

    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is False
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "open"
    assert status.opened_at is not None


async def test_denies_every_request_during_the_cooldown_and_no_call_is_implied(
    breaker: CircuitBreaker, clock: _FakeClock
):
    for _ in range(3):
        await breaker.record_failure(provider="openai", model="gpt-5.4")

    clock.advance(5.0)  # < cooldown (10.0)
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is False
    clock.advance(4.9)  # still < cooldown, cumulative 9.9s
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is False


async def test_transitions_to_half_open_after_cooldown_and_allows_exactly_one_probe(
    breaker: CircuitBreaker, clock: _FakeClock
):
    for _ in range(3):
        await breaker.record_failure(provider="openai", model="gpt-5.4")

    clock.advance(10.1)  # > cooldown

    # The cooldown having elapsed doesn't mean "everyone gets through" —
    # it means exactly one caller becomes the probe.
    first = await breaker.allow_request(provider="openai", model="gpt-5.4")
    second = await breaker.allow_request(provider="openai", model="gpt-5.4")
    third = await breaker.allow_request(provider="openai", model="gpt-5.4")

    assert first is True, "the first caller after cooldown must be admitted as the probe"
    assert second is False, "a second concurrent caller must not also become a probe"
    assert third is False

    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "half_open"


async def test_probe_success_closes_the_circuit_and_resets_the_window(
    breaker: CircuitBreaker, clock: _FakeClock
):
    for _ in range(3):
        await breaker.record_failure(provider="openai", model="gpt-5.4")
    clock.advance(10.1)
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True  # the probe

    await breaker.record_success(provider="openai", model="gpt-5.4")

    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "closed"
    assert status.opened_at is None
    assert status.failures_in_window == 0

    # Fully recovered: normal traffic flows again without needing another
    # cooldown, and the old failures don't linger and re-trip it.
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True


async def test_probe_failure_reopens_the_circuit(breaker: CircuitBreaker, clock: _FakeClock):
    for _ in range(3):
        await breaker.record_failure(provider="openai", model="gpt-5.4")
    clock.advance(10.1)
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True  # the probe

    await breaker.record_failure(provider="openai", model="gpt-5.4")

    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "open"
    assert status.opened_at == clock.now, "reopening resets the cooldown clock from the probe failure"

    # Immediately re-checking (no time elapsed) must still deny — a failed
    # probe does not grant a fresh grace period.
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is False


async def test_window_is_sliding_not_a_lifetime_counter(breaker: CircuitBreaker):
    """window_size=5, threshold=3: two old failures should age out of the
    window rather than accumulate forever toward tripping the circuit."""
    await breaker.record_failure(provider="openai", model="gpt-5.4")
    await breaker.record_failure(provider="openai", model="gpt-5.4")
    await breaker.record_success(provider="openai", model="gpt-5.4")
    await breaker.record_success(provider="openai", model="gpt-5.4")
    await breaker.record_success(provider="openai", model="gpt-5.4")
    # window now (oldest->newest): [F, F, S, S, S] — 2 failures, still closed.
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True

    # One more failure pushes the window to [F, S, S, S, F] (oldest F
    # trimmed off by LTRIM) — still only 2 failures in the 5-entry window,
    # must stay closed rather than spuriously tripping on a "3rd failure
    # ever" basis.
    await breaker.record_failure(provider="openai", model="gpt-5.4")
    status = await breaker.get_status(provider="openai", model="gpt-5.4")
    assert status.failures_in_window == 2
    assert status.state == "closed"


async def test_get_status_reports_provider_model_pairs_independently(breaker: CircuitBreaker):
    for _ in range(3):
        await breaker.record_failure(provider="openai", model="gpt-5.4")
    await breaker.record_failure(provider="anthropic", model="claude-sonnet-5")

    openai_status = await breaker.get_status(provider="openai", model="gpt-5.4")
    anthropic_status = await breaker.get_status(provider="anthropic", model="claude-sonnet-5")

    assert openai_status.state == "open"
    assert anthropic_status.state == "closed"
    assert anthropic_status.failures_in_window == 1


async def test_list_status_covers_every_requested_pair(breaker: CircuitBreaker):
    statuses = await breaker.list_status([("openai", "gpt-5.4"), ("ollama", "llama3.2")])
    assert {s.provider for s in statuses} == {"openai", "ollama"}
    assert all(s.state == "closed" for s in statuses)


async def test_redis_outage_fails_open_on_allow_request(breaker: CircuitBreaker, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise redis_exceptions.ConnectionError("simulated redis outage")

    monkeypatch.setattr(breaker._check_script, "eval", _boom)  # noqa: SLF001

    # A broken circuit-breaker STORE must not make every provider look
    # permanently tripped — see circuit_breaker.py's module docstring.
    assert await breaker.allow_request(provider="openai", model="gpt-5.4") is True


async def test_redis_outage_on_record_does_not_raise(breaker: CircuitBreaker, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise redis_exceptions.ConnectionError("simulated redis outage")

    monkeypatch.setattr(breaker._record_script, "eval", _boom)  # noqa: SLF001

    # Best-effort: recording must not blow up the request that already
    # succeeded/failed on its own terms.
    await breaker.record_success(provider="openai", model="gpt-5.4")
    await breaker.record_failure(provider="openai", model="gpt-5.4")
