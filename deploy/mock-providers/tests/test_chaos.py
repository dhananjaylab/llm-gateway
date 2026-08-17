"""
test_chaos.py

Pure-logic unit tests for ChaosController -- no ASGI, no HTTP, no
adapters. test_mock_provider_wire_compat.py (tests/integration/) already
proves the end-to-end wire shape; this file isolates the
rule-resolution/precedence rules that file doesn't specifically target.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chaos import ChaosController, ChaosInjectedError


@pytest.fixture
def chaos() -> ChaosController:
    return ChaosController()


async def test_no_rule_is_a_silent_no_op(chaos):
    await chaos.apply(provider="openai", model="gpt-5.6-sol")  # must not raise


async def test_error_rate_1_always_raises(chaos):
    chaos.set_rule(provider="openai", model="gpt-5.6-sol", error_rate=1.0)
    with pytest.raises(ChaosInjectedError):
        await chaos.apply(provider="openai", model="gpt-5.6-sol")


async def test_error_rate_0_never_raises(chaos):
    chaos.set_rule(provider="openai", model="gpt-5.6-sol", error_rate=0.0)
    await chaos.apply(provider="openai", model="gpt-5.6-sol")  # must not raise


async def test_exact_model_rule_wins_over_wildcard(chaos):
    """openai:* says 'always fail', openai:gpt-5.6-sol says 'never fail'
    -- the exact match must win, so a test can break one specific model
    in a tier's fallback chain while leaving every other model on the
    same provider healthy."""
    chaos.set_rule(provider="openai", model="*", error_rate=1.0)
    chaos.set_rule(provider="openai", model="gpt-5.6-sol", error_rate=0.0)

    await chaos.apply(provider="openai", model="gpt-5.6-sol")  # exact match: healthy
    with pytest.raises(ChaosInjectedError):
        await chaos.apply(provider="openai", model="gpt-5.6-terra")  # falls through to wildcard


async def test_a_rule_for_one_provider_never_affects_another(chaos):
    chaos.set_rule(provider="openai", model="*", error_rate=1.0)
    await chaos.apply(provider="anthropic", model="claude-sonnet-5")  # unaffected, must not raise


async def test_status_code_and_error_type_are_carried_onto_the_exception(chaos):
    chaos.set_rule(provider="ollama", model="*", error_rate=1.0, status_code=500, error_type="boom")
    with pytest.raises(ChaosInjectedError) as exc_info:
        await chaos.apply(provider="ollama", model="llama3.2")
    assert exc_info.value.status_code == 500
    assert exc_info.value.error_type == "boom"


async def test_latency_only_rule_delays_without_raising(chaos):
    import time

    chaos.set_rule(provider="ollama", model="*", latency_ms=80, error_rate=0.0)
    start = time.monotonic()
    await chaos.apply(provider="ollama", model="llama3.2")
    assert time.monotonic() - start >= 0.075


def test_clear_one_provider_leaves_others_untouched(chaos):
    chaos.set_rule(provider="openai", model="*", error_rate=1.0)
    chaos.set_rule(provider="anthropic", model="*", error_rate=1.0)

    chaos.clear(provider="openai")

    snapshot = chaos.snapshot()
    assert "openai:*" not in snapshot
    assert "anthropic:*" in snapshot


def test_clear_with_no_provider_wipes_everything(chaos):
    chaos.set_rule(provider="openai", model="*", error_rate=1.0)
    chaos.set_rule(provider="anthropic", model="*", error_rate=1.0)

    chaos.clear()

    assert chaos.snapshot() == {}


def test_snapshot_reflects_every_active_rule(chaos):
    chaos.set_rule(provider="openai", model="gpt-5.6-sol", error_rate=0.5, latency_ms=200)
    snapshot = chaos.snapshot()
    assert snapshot["openai:gpt-5.6-sol"]["error_rate"] == 0.5
    assert snapshot["openai:gpt-5.6-sol"]["latency_ms"] == 200
