"""
test_trace_shape_on_fallback.py

Verifies the Phase 4 test plan: "A forced-failover scenario produces one
ERROR-status CLIENT span, one OK-status CLIENT span, and an OK-status root
SERVER span" -- Document 05's own worked trace example (Journey B),
reproduced almost verbatim:

    Span 1 (Root): POST /v1/chat/completions (Kind: SERVER, Status: OK)
    Span 1.1: Authentication & Rate Limit Check (Kind: INTERNAL, Status: OK)
    Span 1.2: chat gpt-4o (Kind: CLIENT, Status: ERROR, error.type: "...")
    Span 1.3: chat claude-3-5-sonnet (Kind: CLIENT, Status: OK, gen_ai.usage...)

"OK-status root SERVER span" here means the OTel-idiomatic equivalent:
UNSET, not ERROR -- see test_span_attributes.py's note on why this
codebase leaves successful spans UNSET rather than force-setting OK. The
invariant Document 05 actually cares about ("a routine failover never
reads as an application-level error") is that the root span is NOT
ERROR despite a child CLIENT span being ERROR -- that's what's asserted
below, not a literal StatusCode.OK equality.
"""

from __future__ import annotations

from opentelemetry.trace import SpanKind, StatusCode

from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client


def _by_name(spans, name: str):
    matches = [s for s in spans if s.name == name]
    assert matches, f"no span named {name!r} among {[s.name for s in spans]}"
    return matches[0]


async def test_non_streaming_failover_produces_the_document_05_span_tree(
    traced_app, span_exporter, monkeypatch
):
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="rate_limit_exceeded")
    working = FakeAdapter(response_text="answer from the backup provider")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.6-sol":
            return failing, "gpt-5.6-sol-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(model_id)

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(traced_app) as client:
        await traced_app.state.team_store.update_team(
            "data-science", {"allowed_models": ["tier-1-reasoning"]}
        )
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )

    assert resp.status_code == 200

    spans = span_exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "POST /v1/chat/completions" in names
    assert "auth.rate_limit_check" in names
    assert "chat gpt-5.6-sol-served" in names
    assert "chat claude-sonnet-5-served" in names

    root = _by_name(spans, "POST /v1/chat/completions")
    failed_client = _by_name(spans, "chat gpt-5.6-sol-served")
    ok_client = _by_name(spans, "chat claude-sonnet-5-served")

    # Span 1.2: the failed primary.
    assert failed_client.kind == SpanKind.CLIENT
    assert failed_client.status.status_code == StatusCode.ERROR
    assert failed_client.attributes["error.type"] == "rate_limit_exceeded"
    assert failed_client.attributes["gen_ai.provider.name"] == "fake"

    # Span 1.3: the successful fallback.
    assert ok_client.kind == SpanKind.CLIENT
    assert ok_client.status.status_code == StatusCode.OK
    assert ok_client.attributes["gen_ai.response.model"] == "claude-sonnet-5-served"
    assert ok_client.attributes["gen_ai.usage.output_tokens"] == 5

    # Both share the same trace and the same parent (the SERVER root),
    # not each other -- siblings, per Document 05's tree, not nested.
    assert failed_client.context.trace_id == root.context.trace_id == ok_client.context.trace_id
    assert failed_client.parent.span_id == root.context.span_id
    assert ok_client.parent.span_id == root.context.span_id

    # The core Journey B invariant: the client got a 200, so the root
    # span must not read as an error, regardless of the failed child.
    assert root.attributes["http.status_code"] == 200
    assert root.status.status_code != StatusCode.ERROR


async def test_streaming_failover_before_first_chunk_produces_the_same_shape(
    traced_app, span_exporter, monkeypatch
):
    """Document 03: fallback for streaming is scoped to failures before any
    content chunk reaches the client -- the pre-first-chunk retry/failover
    phase still produces the same two-CLIENT-span shape as non-streaming."""
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    working = FakeAdapter(stream_chunks=["Hel", "lo"])

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.6-sol":
            return failing, "gpt-5.6-sol-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(model_id)

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(traced_app) as client:
        await traced_app.state.team_store.update_team(
            "data-science", {"allowed_models": ["tier-1-reasoning"]}
        )
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "tier-1-reasoning",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        ) as resp:
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

    spans = span_exporter.get_finished_spans()
    failed_client = _by_name(spans, "chat gpt-5.6-sol-served")
    ok_client = _by_name(spans, "chat claude-sonnet-5-served")
    assert failed_client.status.status_code == StatusCode.ERROR
    assert ok_client.status.status_code == StatusCode.OK
    # Streaming's usage is known only per-chunk; the terminal usage chunk
    # (2 output chunks, 10 input/2 output per FakeAdapter's default) must
    # have landed on the span before it closed.
    assert ok_client.attributes["gen_ai.usage.output_tokens"] == 2
