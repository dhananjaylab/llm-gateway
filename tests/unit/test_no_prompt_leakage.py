"""
test_no_prompt_leakage.py

Verifies the Phase 4 test plan: "With span-event content capture disabled
(the default), no prompt or completion text appears anywhere in exported
spans or metrics" -- plus, to prove this isn't trivially passing because
the mechanism is simply broken, that content DOES appear when the
operator explicitly opts in (OTEL_CAPTURE_MESSAGE_CONTENT=true).

Uses a deliberately distinctive, unlikely-to-appear-by-accident marker
string as the "secret" so a false negative (the assertion passing only
because it searched for the wrong text) is implausible.
"""

from __future__ import annotations

from app.observability.tracing import span_contains_no_content
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter

_SECRET_MARKER = "xyzzy-plugh-do-not-leak-4471"


def _post(client, monkeypatch, fake, *, text: str = _SECRET_MARKER):
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    return client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": text}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )


def test_content_capture_is_off_by_default(traced_client, span_exporter, monkeypatch):
    fake = FakeAdapter(response_text=_SECRET_MARKER)
    resp = _post(traced_client, monkeypatch, fake)
    assert resp.status_code == 200

    spans = span_exporter.get_finished_spans()
    assert spans, "expected at least one span to inspect"
    for span in spans:
        assert span_contains_no_content(span), f"content-capture event leaked onto {span.name!r}"
        for attr_value in span.attributes.values():
            assert _SECRET_MARKER not in str(attr_value), (
                f"prompt/completion text leaked into a span ATTRIBUTE on {span.name!r} "
                "-- the TRD explicitly forbids this even with capture enabled, let alone disabled"
            )


def test_content_capture_never_leaks_into_prometheus_metrics(client, monkeypatch):
    """Metrics are label/value pairs, not free text -- this is really a
    belt-and-suspenders check that no label value ever becomes the
    message content itself (e.g. a bug that used response text as a
    label)."""
    fake = FakeAdapter(response_text=_SECRET_MARKER)
    resp = _post(client, monkeypatch, fake)
    assert resp.status_code == 200

    body = client.get("/metrics").text
    assert _SECRET_MARKER not in body


def test_content_capture_appears_when_explicitly_enabled(monkeypatch):
    """Proves the opt-in path is real, not just untested. Rebuilds a fresh
    app with OTEL_CAPTURE_MESSAGE_CONTENT=true rather than reusing the
    `traced_client` fixture, since the flag is read once at
    FallbackRouter-construction time (inside the lifespan) -- flipping the
    env var after an app already exists wouldn't retroactively change its
    already-built FallbackRouter."""
    import fakeredis
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.core import config as config_module
    from app.main import create_app

    monkeypatch.setenv("OTEL_CAPTURE_MESSAGE_CONTENT", "true")
    config_module.reset_gateway_settings_cache()

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    exporter = InMemorySpanExporter()
    app = create_app(redis_client=redis, span_exporter_override=exporter)

    fake = FakeAdapter(response_text=_SECRET_MARKER)

    def _resolve(model_id: str):
        return fake, "served-model"

    import app.api.v1_chat as v1_chat_module

    monkeypatch.setattr(v1_chat_module, "resolve_model", _resolve)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": _SECRET_MARKER}]},
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )
        assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    client_span = next(s for s in spans if s.name == "chat served-model")
    assert not span_contains_no_content(client_span), (
        "expected the content-capture event to be present once explicitly enabled -- "
        "if this fails, the opt-in mechanism itself is broken, not just its default-off state"
    )
    event = next(e for e in client_span.events if e.name == "gateway.content.capture")
    payload = event.attributes["gateway.content.json"]
    assert _SECRET_MARKER in payload

    config_module.reset_gateway_settings_cache()
