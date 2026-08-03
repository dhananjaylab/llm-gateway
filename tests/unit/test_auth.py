"""
test_auth.py

Verifies: "Valid key -> 200 path continues; invalid key -> 401; valid key,
disallowed model -> 403" — per the Phase 1 test plan.
"""

from __future__ import annotations

from tests.unit.conftest import BATCH_DEVS_KEY, DATA_SCIENCE_KEY, FakeAdapter


def _body(model: str = "openai:gpt-5.4") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


def test_missing_api_key_header_is_rejected(client):
    resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 422  # FastAPI: required header missing


def test_invalid_api_key_returns_401(client):
    resp = client.post(
        "/v1/chat/completions",
        json=_body(),
        headers={"X-Gateway-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"]["type"] == "invalid_api_key"


def test_valid_key_disallowed_model_returns_403(client):
    # batch-devs is only allowed ollama:llama3.2 per config/teams.yaml
    resp = client.post(
        "/v1/chat/completions",
        json=_body(model="openai:gpt-5.4"),
        headers={"X-Gateway-API-Key": BATCH_DEVS_KEY},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"]["type"] == "model_not_allowed"


def test_valid_key_allowed_model_reaches_the_provider_adapter(client, monkeypatch):
    fake = FakeAdapter(response_text="hi from data-science")
    monkeypatch.setattr(
        "app.api.v1_chat.resolve_model", lambda model_id: (fake, "gpt-5.4-served")
    )

    resp = client.post(
        "/v1/chat/completions",
        json=_body(model="openai:gpt-5.4"),
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "hi from data-science"
    assert data["provider"] == "fake"
    assert fake.call_count == 1


def test_unknown_provider_prefix_returns_400(client, monkeypatch):
    # Simulate a model string with a provider prefix that has no configured
    # adapter (e.g. its API key was never set) — registry.resolve_model
    # raises UnknownProviderError, which the route maps to 400.
    from app.core.config import get_gateway_config

    team = get_gateway_config().teams["data-science"]
    # Temporarily allow a bogus provider so we get past the 403 check and
    # exercise the registry's 400 branch instead.
    team.allowed_models.append("nonexistent:some-model")

    resp = client.post(
        "/v1/chat/completions",
        json=_body(model="nonexistent:some-model"),
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["type"] == "unknown_provider"
