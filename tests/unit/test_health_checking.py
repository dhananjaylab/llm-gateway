"""
test_health_checking.py

Verifies the Phase 3 test plan: "Synthetic probes update provider status;
a provider with elevated passive-window error rate is marked degraded
even if the last active probe succeeded."

Unit-level against `HealthTracker`/`probe_once`/`HealthChecker` directly
(fakeredis, injectable clock) for the same reason test_circuit_breaker.py
is unit-level — precise, fast, deterministic control over the rolling
window's contents. GET /admin/health's read side (the admin-visibility
addition) is covered separately in test_admin_resilience.py; the
background task actually running inside the app's lifespan is covered by
main.py wiring plus the `HEALTH_CHECK_ENABLED=false` test-suite default
documented in conftest.py.
"""

from __future__ import annotations

import asyncio

import pytest

from app.providers.registry import all_configured_provider_models
from app.resilience.health import HealthChecker, HealthTracker, probe_once
from tests.unit.conftest import FakeAdapter


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
def tracker(fake_redis, clock: _FakeClock) -> HealthTracker:
    return HealthTracker(
        fake_redis,
        window_seconds=60,
        degraded_error_rate=0.3,
        down_error_rate=0.8,
        degraded_latency_p99_ms=500.0,
        clock=clock,
    )


async def _record(tracker: HealthTracker, clock: _FakeClock, *, ok: bool, latency_ms: float = 100.0) -> None:
    await tracker.record_outcome(provider="openai", model="gpt-5.4", ok=ok, latency_ms=latency_ms)
    clock.advance(1.0)


# -- status derivation ---------------------------------------------------


async def test_unknown_before_any_data_exists(tracker: HealthTracker):
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "unknown"
    assert status.sample_count == 0
    assert status.error_rate is None


async def test_all_successes_is_healthy(tracker: HealthTracker, clock: _FakeClock):
    for _ in range(5):
        await _record(tracker, clock, ok=True)
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.state == "healthy"
    assert status.error_rate == 0.0
    assert status.sample_count == 5


async def test_error_rate_at_the_degraded_threshold_marks_degraded(tracker: HealthTracker, clock: _FakeClock):
    # 3 failures / 10 samples = 0.3 == degraded_error_rate exactly.
    for ok in [False] * 3 + [True] * 7:
        await _record(tracker, clock, ok=ok)
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.error_rate == pytest.approx(0.3)
    assert status.state == "degraded"


async def test_error_rate_at_the_down_threshold_marks_down(tracker: HealthTracker, clock: _FakeClock):
    # 8 failures / 10 samples = 0.8 == down_error_rate exactly.
    for ok in [False] * 8 + [True] * 2:
        await _record(tracker, clock, ok=ok)
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.error_rate == pytest.approx(0.8)
    assert status.state == "down"


async def test_high_p99_latency_marks_degraded_even_with_zero_errors(
    tracker: HealthTracker, clock: _FakeClock
):
    for _ in range(5):
        await _record(tracker, clock, ok=True, latency_ms=999_999.0)
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.error_rate == 0.0
    assert status.state == "degraded"


async def test_elevated_passive_error_rate_marks_degraded_even_if_the_most_recent_probe_succeeded(
    tracker: HealthTracker, clock: _FakeClock
):
    """The TRD's own Phase 3 test-plan line, verified directly: real
    traffic (passive monitoring) accumulates 3 failures, and the single
    most recent observation — standing in for 'the last active probe' —
    is a success. The window as a whole is still degraded."""
    for _ in range(3):
        await _record(tracker, clock, ok=False)
    await _record(tracker, clock, ok=True)  # 2 successes total after this + one below
    await _record(tracker, clock, ok=True)
    # window: [F, F, F, S, S] -> error_rate 0.6, above degraded(0.3), below down(0.8)
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.error_rate == pytest.approx(0.6)
    assert status.state == "degraded"


async def test_window_trims_entries_older_than_window_seconds(tracker: HealthTracker, clock: _FakeClock):
    await tracker.record_outcome(provider="openai", model="gpt-5.4", ok=False, latency_ms=100)
    clock.advance(61.0)  # past window_seconds=60
    await tracker.record_outcome(provider="openai", model="gpt-5.4", ok=True, latency_ms=100)

    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.sample_count == 1, "the old failure must have aged out of the window"
    assert status.state == "healthy"


async def test_list_status_covers_every_requested_pair_independently(
    tracker: HealthTracker, clock: _FakeClock
):
    await _record(tracker, clock, ok=True)
    statuses = await tracker.list_status([("openai", "gpt-5.4"), ("ollama", "llama3.2")])
    by_model = {s.model: s for s in statuses}
    assert by_model["gpt-5.4"].state == "healthy"
    assert by_model["llama3.2"].state == "unknown"


# -- probe_once ------------------------------------------------------------


async def test_probe_once_records_a_successful_outcome(tracker: HealthTracker, clock: _FakeClock):
    adapter = FakeAdapter(response_text="pong")
    await probe_once(
        tracker,
        provider_model_id="openai:gpt-5.4",
        provider_name="openai",
        provider_model="gpt-5.4",
        adapter=adapter,
        timeout_seconds=1.0,
        clock=clock,
    )
    assert adapter.call_count == 1
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.sample_count == 1
    assert status.error_rate == 0.0


async def test_probe_once_records_a_failed_outcome_without_raising(tracker: HealthTracker, clock: _FakeClock):
    adapter = FakeAdapter(always_fail=True, retryable=False, error_type="rate_limit_exceeded")
    await probe_once(
        tracker,
        provider_model_id="openai:gpt-5.4",
        provider_name="openai",
        provider_model="gpt-5.4",
        adapter=adapter,
        timeout_seconds=1.0,
        clock=clock,
    )
    status = await tracker.get_status(provider="openai", model="gpt-5.4")
    assert status.error_rate == 1.0


async def test_probe_once_treats_a_timeout_as_a_failure(tracker: HealthTracker, clock: _FakeClock):
    adapter = FakeAdapter(latency_seconds=1.0)
    await probe_once(
        tracker,
        provider_model_id="ollama:llama3.2",
        provider_name="ollama",
        provider_model="llama3.2",
        adapter=adapter,
        timeout_seconds=0.05,
        clock=clock,
    )
    status = await tracker.get_status(provider="ollama", model="llama3.2")
    assert status.error_rate == 1.0


async def test_probe_once_sends_a_minimal_single_token_ping(tracker: HealthTracker, clock: _FakeClock):
    """TRD: 'generating a single token from a baseline prompt like ping'."""
    adapter = FakeAdapter(response_text="pong")
    await probe_once(
        tracker,
        provider_model_id="openai:gpt-5.4",
        provider_name="openai",
        provider_model="gpt-5.4",
        adapter=adapter,
        timeout_seconds=1.0,
        clock=clock,
    )
    sent = adapter.last_translated_request
    assert sent is not None
    assert sent.max_tokens == 16
    assert sent.messages[0].content == "ping"
    assert sent.stream is False


# -- HealthChecker (the background daemon's per-round logic) -----------------


async def test_run_one_round_probes_every_configured_pair_exactly_once(tracker: HealthTracker):
    adapters = {
        "openai:gpt-5.4": FakeAdapter(response_text="ok-openai"),
        "anthropic:claude-sonnet-5": FakeAdapter(response_text="ok-anthropic"),
    }

    def _resolve(model_id: str):
        provider, _, provider_model = model_id.partition(":")
        return adapters[model_id], provider_model

    checker = HealthChecker(
        tracker,
        provider_models=[
            ("openai:gpt-5.4", "openai", "gpt-5.4"),
            ("anthropic:claude-sonnet-5", "anthropic", "claude-sonnet-5"),
        ],
        interval_seconds=1000.0,
        probe_timeout_seconds=1.0,
    )

    await checker._run_one_round(_resolve)  # noqa: SLF001 - testing the round directly, not the sleep loop

    for adapter in adapters.values():
        assert adapter.call_count == 1

    # FakeAdapter's provider_name is always "fake" regardless of which
    # chain link it's standing in for (see conftest.py) — the model
    # string is what distinguishes the two pairs here.
    openai_status = await tracker.get_status(provider="fake", model="gpt-5.4")
    anthropic_status = await tracker.get_status(provider="fake", model="claude-sonnet-5")
    assert openai_status.sample_count == 1
    assert anthropic_status.sample_count == 1


async def test_run_one_round_skips_a_pair_whose_resolver_blows_up_without_crashing_the_round(
    tracker: HealthTracker,
):
    good = FakeAdapter(response_text="ok")

    def _resolve(model_id: str):
        if model_id == "broken:model":
            raise RuntimeError("simulated resolver bug")
        return good, "gpt-5.4"

    checker = HealthChecker(
        tracker,
        provider_models=[("broken:model", "broken", "model"), ("openai:gpt-5.4", "openai", "gpt-5.4")],
        interval_seconds=1000.0,
        probe_timeout_seconds=1.0,
    )

    await checker._run_one_round(_resolve)  # noqa: SLF001

    assert good.call_count == 1
    status = await tracker.get_status(provider="fake", model="gpt-5.4")
    assert status.sample_count == 1


async def test_run_forever_probes_on_an_interval_and_can_be_cancelled_cleanly(tracker: HealthTracker):
    adapter = FakeAdapter(response_text="ok")
    checker = HealthChecker(
        tracker,
        provider_models=[("openai:gpt-5.4", "openai", "gpt-5.4")],
        interval_seconds=0.02,
        probe_timeout_seconds=1.0,
    )
    task = asyncio.create_task(checker.run_forever(lambda model_id: (adapter, "gpt-5.4")))
    await asyncio.sleep(0.09)  # enough time for a handful of rounds
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.call_count >= 2, "the loop should have probed more than once across ~4 intervals"


# -- registry helper (used by main.py to build the probe list) ---------------


def test_all_configured_provider_models_skips_providers_with_no_api_key(monkeypatch):
    from app.providers import registry as registry_module

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    registry_module.reset_registry_cache()

    resolved = all_configured_provider_models(
        ["openai:gpt-5.4", "gemini:gemini-3.6-flash", "ollama:llama3.2", "not-even-valid"]
    )
    model_ids = {r[0] for r in resolved}
    assert "openai:gpt-5.4" in model_ids
    assert "ollama:llama3.2" in model_ids  # always registered, no key required
    assert "gemini:gemini-3.6-flash" not in model_ids  # key unset -> silently skipped
    assert "not-even-valid" not in model_ids  # malformed -> silently skipped

    registry_module.reset_registry_cache()
