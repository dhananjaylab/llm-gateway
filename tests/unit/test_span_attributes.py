"""
test_span_attributes.py

Verifies the Phase 4 test plan: "Every gen_ai.* attribute in Document 05's
schema table is present and correctly typed on the CLIENT span for a
successful call" -- plus the INTERNAL auth.rate_limit_check span and the
root SERVER span's own shape.

Uses the `traced_client`/`span_exporter` fixtures (tests/unit/conftest.py)
-- a real `InMemorySpanExporter` wired via `SimpleSpanProcessor`, so these
assert against spans OpenTelemetry's SDK actually produced, not a mock of
what we think it should produce.
"""

from __future__ import annotations

from opentelemetry.trace import SpanKind, StatusCode

from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter


def _find(spans, name: str):
    matches = [s for s in spans if s.name == name]
    assert matches, f"no span named {name!r} among {[s.name for s in spans]}"
    return matches[0]


def test_client_span_carries_the_full_gen_ai_attribute_set_on_success(
    traced_client, span_exporter, monkeypatch
):
    fake = FakeAdapter(response_text="hi from data-science")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "gpt-5.4-served"))

    resp = traced_client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200

    spans = span_exporter.get_finished_spans()
    client_span = _find(spans, "chat gpt-5.4-served")

    assert client_span.kind == SpanKind.CLIENT
    assert client_span.status.status_code == StatusCode.OK
    attrs = client_span.attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "fake"
    assert attrs["gen_ai.request.model"] == "gpt-5.4-served"
    assert attrs["gen_ai.response.model"] == "gpt-5.4-served"
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["gen_ai.client.team_id"] == "data-science"
    # error.type must never appear on a successful span.
    assert "error.type" not in attrs


def test_client_span_carries_error_type_on_a_non_retryable_failure(traced_client, span_exporter, monkeypatch):
    failing = FakeAdapter(always_fail=True, retryable=False, error_type="invalid_request")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (failing, "gpt-5.4-served"))

    resp = traced_client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 502

    spans = span_exporter.get_finished_spans()
    client_span = _find(spans, "chat gpt-5.4-served")
    assert client_span.status.status_code == StatusCode.ERROR
    assert client_span.attributes["error.type"] == "invalid_request"
    # A non-retryable failure aborts the whole chain -- gen_ai.response.model
    # is only ever set on the success path, so it must be entirely absent
    # here, not just falsy.
    assert "gen_ai.response.model" not in client_span.attributes


def test_healthz_readyz_metrics_are_excluded_from_tracing(traced_client, span_exporter):
    """Polled every few seconds by container orchestration/Prometheus --
    tracing them would flood every trace backend with high-frequency,
    zero-value spans. See tracing.py's excluded_urls."""
    traced_client.get("/healthz")
    traced_client.get("/readyz")
    traced_client.get("/metrics")

    spans = span_exporter.get_finished_spans()
    assert not any("healthz" in s.name or "readyz" in s.name or "metrics" in s.name for s in spans)


def test_internal_auth_span_wraps_every_successful_request(traced_client, span_exporter, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "gpt-5.4-served"))

    traced_client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )

    spans = span_exporter.get_finished_spans()
    auth_span = _find(spans, "auth.rate_limit_check")
    assert auth_span.kind == SpanKind.INTERNAL
    assert auth_span.attributes["gen_ai.client.team_id"] == "data-science"
    # Per OTel convention (and this codebase's own choice, documented in
    # app/observability/tracing.py): a successful INTERNAL span is left
    # UNSET, not force-set to OK -- OK is reserved for explicitly
    # overriding an auto-recorded ERROR. UNSET reads identically to "no
    # error" in every tracing backend.
    assert auth_span.status.status_code == StatusCode.UNSET


def test_internal_auth_span_reports_error_on_a_403_model_not_allowed(traced_client, span_exporter):
    """batch-devs is only allowed ollama:llama3.2 per config/teams.yaml."""
    from tests.unit.conftest import BATCH_DEVS_KEY

    resp = traced_client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": BATCH_DEVS_KEY},
    )
    assert resp.status_code == 403

    spans = span_exporter.get_finished_spans()
    auth_span = _find(spans, "auth.rate_limit_check")
    # The exception propagated out of the `with internal_span(...)` block
    # unhandled -- OTel's own SDK auto-records it and sets ERROR, with no
    # extra code on our part (see internal_span's docstring).
    assert auth_span.status.status_code == StatusCode.ERROR


def test_root_server_span_carries_the_http_route_and_status(traced_client, span_exporter, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "gpt-5.4-served"))

    traced_client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )

    spans = span_exporter.get_finished_spans()
    root = _find(spans, "POST /v1/chat/completions")
    assert root.kind == SpanKind.SERVER
    assert root.attributes["http.status_code"] == 200
    assert root.attributes["http.route"] == "/v1/chat/completions"
    # No ASGI-protocol-level noise (see tracing.py's exclude_spans).
    other_spans = [s for s in spans if s is not root]
    assert not any(
        s.name.startswith(("GET /", "POST /")) and "http" in s.name for s in other_spans
    )
