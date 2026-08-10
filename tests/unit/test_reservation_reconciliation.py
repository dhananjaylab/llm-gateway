"""
test_reservation_reconciliation.py
"""

from __future__ import annotations

from app.core.schema import Usage
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter


def _remaining_tpm(client, admin_headers) -> int:
    resp = client.get("/admin/limits/data-science", headers=admin_headers)
    assert resp.status_code == 200
    return resp.json()["remaining_tpm"]


def test_unused_reservation_is_refunded_after_a_short_completion(client, monkeypatch, admin_headers):
    client.patch("/admin/limits/data-science", json={"tpm_cap": 1000}, headers=admin_headers)

    fake = FakeAdapter(usage_override=Usage(input_tokens=10, output_tokens=5))  # total=15
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "openai:gpt-5.4",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 900,  # reservation ~= 1 (estimated input) + 900 = 901
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200

    assert _remaining_tpm(client, admin_headers) == 985


def test_reservation_is_fully_refunded_when_the_provider_call_fails(client, monkeypatch, admin_headers):
    """
    A permanently-failing (always_fail) adapter must not leak the
    reservation forever — app/api/v1_chat.py explicitly reconciles with
    actual_tokens=0 once the fallback chain is exhausted, refunding the
    entire reservation back. Phase 3 note: FakeAdapter defaults to
    retryable=True, so this now also exercises the retry-before-giving-up
    path (3 attempts against the single "openai" chain link) before
    surfacing the 503 "fallback chain exhausted" — the reservation refund
    behavior this test is about is unchanged either way.
    """
    client.patch("/admin/limits/data-science", json={"tpm_cap": 1000}, headers=admin_headers)

    fake = FakeAdapter(always_fail=True, retryable=True)
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "openai:gpt-5.4",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 900,
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 503

    # Full refund: bucket should be back to (nearly) full capacity, not
    # stuck at capacity-minus-reservation forever.
    assert _remaining_tpm(client, admin_headers) == 1000


def test_no_refund_and_no_crash_when_actual_usage_exceeds_the_reservation(client, monkeypatch, admin_headers):
    client.patch("/admin/limits/data-science", json={"tpm_cap": 1000}, headers=admin_headers)

    fake = FakeAdapter(usage_override=Usage(input_tokens=500, output_tokens=500))  # total=1000
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "openai:gpt-5.4",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200
    assert _remaining_tpm(client, admin_headers) == 994


async def test_streaming_reservation_is_reconciled_against_the_terminal_usage_chunk(
    client, monkeypatch, admin_headers
):
    client.patch("/admin/limits/data-science", json={"tpm_cap": 1000}, headers=admin_headers)

    fake = FakeAdapter(
        stream_chunks=["a", "b"], usage_override=Usage(input_tokens=10, output_tokens=10)
    )  # total=20
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "openai:gpt-5.4",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 900,
            "stream": True,
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    ) as resp:
        assert resp.status_code == 200
        resp.read()

    assert _remaining_tpm(client, admin_headers) == 980  # 1000 - 20
