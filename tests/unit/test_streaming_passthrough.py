"""
test_streaming_passthrough.py

Verifies: "SSE chunks arrive at the client in order and unmodified in
content; TTFT is captured; a mocked mid-stream client disconnect cancels
the upstream call" — per the Phase 1 test plan, still true in Phase 3.

Phase 3 change: `_stream_response` now takes `fallback_router` + `chain`
instead of a bare `adapter` + `provider_model` (see app/api/v1_chat.py's
module docstring). The disconnect test below builds a real
`FallbackRouter` wired with `app.resilience.stub.CircuitBreaker` (the
Phase 1 always-Closed stub, kept around for exactly this — see its
docstring) and a single-attempt `RetryPolicy`, rather than a
fakeredis-backed one: this test is about disconnect handling, not
circuit-breaker/retry mechanics, so keeping it Redis-free is deliberate.
"""

from __future__ import annotations

import json
import logging

from app.api.v1_chat import _stream_response
from app.core.config import TiersConfig
from app.core.schema import ChatMessage, UnifiedChatRequest
from app.resilience.fallback import FallbackRouter
from app.resilience.retry import RetryPolicy
from app.resilience.stub import CircuitBreaker as AlwaysClosedCircuitBreaker
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter


def _parse_sse(raw_text: str) -> list[dict]:
    """Pull every `data: {...}` frame out of a raw SSE response body, in order."""
    events = []
    for block in raw_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def test_stream_chunks_arrive_in_order_and_unmodified(client, monkeypatch):
    fake = FakeAdapter(stream_chunks=["The", " quick", " fox"])
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    body = {
        "model": "openai:gpt-5.4",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json=body,
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        raw_text = resp.read().decode()

    events = _parse_sse(raw_text)
    deltas = [e["delta"] for e in events if e["delta"]]
    assert deltas == ["The", " quick", " fox"]
    # Terminal chunk carries usage and a finish_reason, content-empty.
    assert events[-1]["finish_reason"] == "stop"
    assert events[-1]["usage"]["output_tokens"] == 3


def test_ttft_is_logged_on_first_chunk(client, monkeypatch, caplog):
    fake = FakeAdapter(stream_chunks=["only-chunk"])
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    body = {
        "model": "openai:gpt-5.4",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    with caplog.at_level(logging.INFO, logger="gateway.v1_chat"), client.stream(
        "POST",
        "/v1/chat/completions",
        json=body,
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    ) as resp:
        resp.read()

    ttft_records = [r for r in caplog.records if r.getMessage() == "time_to_first_token"]
    assert len(ttft_records) == 1
    assert ttft_records[0].ttft_ms >= 0


class _FakeDisconnectingRequest:
    """
    Stands in for Starlette's Request: reports "connected" for the first
    `disconnect_after` checks, then "disconnected" from then on.
    """

    def __init__(self, disconnect_after: int) -> None:
        self._calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._disconnect_after


class _TrackingAdapter(FakeAdapter):
    """Extends FakeAdapter's stream() with a longer chunk list so we can
    prove early-exit actually happened (fewer than all chunks consumed)."""

    def __init__(self) -> None:
        super().__init__(stream_chunks=["c0", "c1", "c2", "c3", "c4"])


class _NoopRateLimiter:
    """Records reconcile() calls without touching Redis — this test is
    about disconnect handling, not rate-limit bookkeeping."""

    def __init__(self) -> None:
        self.reconcile_calls: list[tuple[int, int]] = []

    async def reconcile(self, *, team, reserved_tokens, actual_tokens) -> None:
        self.reconcile_calls.append((reserved_tokens, actual_tokens))


class _NoopBudgetEnforcer:
    def __init__(self) -> None:
        self.record_spend_calls: list[float] = []

    async def record_spend(self, team, cost_usd) -> None:
        self.record_spend_calls.append(cost_usd)


def _redis_free_fallback_router() -> FallbackRouter:
    """A FallbackRouter with no Redis dependency at all: the always-Closed
    circuit-breaker stub (never denies, never touches Redis) and a
    single-attempt retry policy (this test's adapter never fails, so
    retries are irrelevant to what's being verified)."""
    return FallbackRouter(
        circuit_breaker=AlwaysClosedCircuitBreaker(),
        retry_policy=RetryPolicy(max_attempts=1),
        tiers_config=TiersConfig(tiers={}),
    )


async def test_client_disconnect_cancels_the_upstream_call(monkeypatch):
    from app.core.config import TeamConfig, TeamPolicy

    adapter = _TrackingAdapter()
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (adapter, "gpt-5.4-served"))

    fake_request = _FakeDisconnectingRequest(disconnect_after=1)
    unified_request = UnifiedChatRequest(
        model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")], stream=True
    )
    team = TeamConfig(
        team_id="data-science",
        api_key_hash="sha256:irrelevant-for-this-test",
        allowed_models=["openai:gpt-5.4"],
        policy=TeamPolicy(),
    )
    rate_limiter = _NoopRateLimiter()
    budget_enforcer = _NoopBudgetEnforcer()
    fallback_router = _redis_free_fallback_router()

    received: list = []  # list[str], the SSE-frame strings _stream_response yields
    async for event in _stream_response(
        fallback_router=fallback_router,
        chain=["openai:gpt-5.4"],
        tier_or_model="openai:gpt-5.4",
        request=unified_request,
        http_request=fake_request,
        team=team,
        reserved_tokens=42,
        rate_limiter=rate_limiter,
        budget_enforcer=budget_enforcer,
        pricing_table={},
    ):
        received.append(event)

    # Exactly one SSE event reached the "client" before disconnect was
    # detected and the loop broke.
    assert len(received) == 1
    # The upstream generator was cut off early — it did not run to
    # completion (5 content chunks + 1 terminal usage chunk = 6 total).
    assert adapter.yielded_count < 6
    # And its cleanup path (aclose()) ran, proving the upstream call was
    # actually cancelled rather than left dangling — FallbackRouter's own
    # `finally: await agen.aclose()` (app/resilience/fallback.py) closes
    # the raw adapter stream when v1_chat.py's outer `agen.aclose()` (also
    # a `finally`) closes the fallback-aware wrapper generator around it.
    assert adapter.stream_closed is True
    # Phase 7 behavior (changed from Phase 2's blanket-zero refund): the
    # one chunk that DID reach the client ("c0", via the "fake" provider,
    # which falls through to the project's existing 4-chars/token
    # heuristic in app/ratelimit/output_tokenizer.py) is estimated at 1
    # output token, plus the pre-flight input estimate for "hi" (1 token)
    # — so the reservation is now reconciled against 2 actual tokens, not
    # a blanket 0. See docs/PHASE7_IMPLEMENTATION_GUIDE.md ("Advanced
    # Token Accounting for Cancelled and Aborted Streams") and
    # _reconcile_and_bill_partial in app/api/v1_chat.py.
    assert rate_limiter.reconcile_calls == [(42, 2)]
    # And — the actual financial-leakage fix — the partial content is now
    # billed to the team's budget instead of silently never recorded.
    # cost_usd is 0.0 here only because pricing_table={} has no entry for
    # the "fake" provider (see the captured warning log); the call
    # happening at all, with a real (estimated) Usage behind it, is the
    # behavior under test.
    assert budget_enforcer.record_spend_calls == [0.0]
