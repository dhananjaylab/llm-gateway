"""
test_reservation_reconciliation.py

Verifies the Phase 2 test plan: "A request with a high max_tokens but a
short actual completion refunds the unused reservation back into the
bucket."

Technique: pin the team's tpm_cap to a small, exact number via the Admin
API, send a request with a large max_tokens (so the pre-call reservation
consumes most of the bucket), then read the bucket back via the peek
endpoint (GET /admin/limits/{team}, which reuses token_bucket.lua's
non-destructive peek mode) and assert it reflects only the real usage the
FakeAdapter reported — not the full reservation.
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

    # Real usage the "provider" reports is tiny compared to the reservation.
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

    # Bucket should reflect only the 15 tokens actually used, not the 901
    # that were reserved pre-call: 1000 - 15 = 985.
    assert _remaining_tpm(client, admin_headers) == 985


def test_reservation_is_fully_refunded_when_the_provider_call_fails(client, monkeypatch, admin_headers):
    """
    A failed call (adapter raises ProviderError) must not leak the
    reservation forever — app/api/v1_chat.py explicitly reconciles with
    actual_tokens=0 on the ProviderError branch, refunding the entire
    reservation back.
    """
    from app.providers.base import ProviderError

    client.patch("/admin/limits/data-science", json={"tpm_cap": 1000}, headers=admin_headers)

    class _FailingAdapter(FakeAdapter):
        async def call(self, payload: dict) -> dict:
            raise ProviderError("boom", status_code=500, retryable=False, error_type="provider_error")

    fake = _FailingAdapter()
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
    assert resp.status_code == 502

    # Full refund: bucket should be back to (nearly) full capacity, not
    # stuck at capacity-minus-reservation forever.
    assert _remaining_tpm(client, admin_headers) == 1000


def test_no_refund_and_no_crash_when_actual_usage_exceeds_the_reservation(client, monkeypatch, admin_headers):
    """
    reconcile_tpm.lua's documented behavior: if actual usage is >= what
    was reserved, the overage is silently accepted (the response was
    already sent), not clawed back from elsewhere in the bucket, and the
    call must not raise.
    """
    client.patch("/admin/limits/data-science", json={"tpm_cap": 1000}, headers=admin_headers)

    # max_tokens=5 -> reservation ~= 1 + 5 = 6, but the "provider" reports
    # far more usage than that (a pathological/unrealistic case, but the
    # code must not misbehave on it).
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
    # Only the ~6-token reservation was ever consumed from the bucket;
    # the script must not attempt (and must not crash attempting) to
    # claw back the 994-token overage from elsewhere.
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
