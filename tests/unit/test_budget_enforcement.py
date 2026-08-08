"""
test_budget_enforcement.py

Verifies the Phase 2 test plan: "Spend crossing 80% triggers the warning
header/webhook exactly once per threshold crossing; spend at 100% blocks
with 402/403 and a clear reason" — plus Document 03 Journey C's
requirement that the budget check happens "before any upstream provider
call is made (no wasted spend on a request that will be blocked)".

Cost control technique: FakeAdapter's `usage_override` plus a pricing
entry injected directly into `app.state.pricing` gives an exact,
predictable dollar cost per call (see `_set_fixed_cost_per_call`) —
avoids needing real provider usage numbers or a real pricing table entry
for the "fake" provider.
"""

from __future__ import annotations

from app.core.pricing import ModelPricing
from app.core.schema import Usage
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter


def _set_fixed_cost_per_call(client, *, cost_usd: float) -> FakeAdapter:
    """
    Registers a FakeAdapter whose every successful call costs exactly
    `cost_usd`, by pairing a large-but-fixed input-token usage with a
    matching pricing rate (input_tokens * input_per_million / 1e6 == cost_usd).
    """
    fake = FakeAdapter(usage_override=Usage(input_tokens=1_000_000, output_tokens=0))
    client.app.state.pricing["fake:served-model"] = ModelPricing(
        input_per_million=cost_usd, output_per_million=0.0
    )
    return fake


def _post(client, monkeypatch, fake):
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    return client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )


def test_warning_header_fires_exactly_once_at_80_percent_crossing(client, monkeypatch, admin_headers):
    patch = client.patch(
        "/admin/budgets/data-science", json={"budget_cap_usd": 10.0}, headers=admin_headers
    )
    assert patch.status_code == 200

    fake = _set_fixed_cost_per_call(client, cost_usd=3.0)

    r1 = _post(client, monkeypatch, fake)  # spend 0 -> 3
    r2 = _post(client, monkeypatch, fake)  # spend 3 -> 6
    r3 = _post(client, monkeypatch, fake)  # spend 6 -> 9  (>= 8.0 = 80% of 10 -> crosses)
    r4 = _post(client, monkeypatch, fake)  # spend 9 -> 12 (already warned; no re-fire)

    for r in (r1, r2, r3, r4):
        assert r.status_code == 200, r.text

    assert "X-Budget-Warning" not in r1.headers
    assert "X-Budget-Warning" not in r2.headers
    assert "X-Budget-Warning" in r3.headers
    assert float(r3.headers["X-Budget-Warning"]) >= 0.8
    assert "X-Budget-Warning" not in r4.headers, "must not re-fire after the first crossing"


def test_hard_cap_blocks_with_402_and_a_clear_reason(client, monkeypatch, admin_headers):
    patch = client.patch(
        "/admin/budgets/data-science", json={"budget_cap_usd": 5.0}, headers=admin_headers
    )
    assert patch.status_code == 200

    fake = _set_fixed_cost_per_call(client, cost_usd=3.0)

    r1 = _post(client, monkeypatch, fake)  # spend 0 -> 3, allowed (0 < 5)
    r2 = _post(client, monkeypatch, fake)  # spend 3 -> 6, allowed (3 < 5, even though it crosses)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert fake.call_count == 2

    r3 = _post(client, monkeypatch, fake)  # precheck: 6 >= 5 -> blocked before any call
    assert r3.status_code == 402
    body = r3.json()["detail"]["error"]
    assert body["type"] == "budget_exceeded"
    assert body["cap_usd"] == 5.0
    assert body["spend_usd"] == 6.0
    assert "period" in body

    # The whole point of a precheck: the provider adapter must never have
    # been invoked a 3rd time for the request that got blocked.
    assert fake.call_count == 2


def test_budget_precheck_never_calls_the_provider_when_already_over_cap(
    client, monkeypatch, admin_headers
):
    """Isolates Document 03 Journey C's "no wasted spend" guarantee as its
    own test, independent of the warning/crossing mechanics above."""
    client.patch("/admin/budgets/data-science", json={"budget_cap_usd": 1.0}, headers=admin_headers)
    fake = _set_fixed_cost_per_call(client, cost_usd=5.0)

    r1 = _post(client, monkeypatch, fake)  # spend 0 -> 5, allowed (0 < 1)
    assert r1.status_code == 200
    assert fake.call_count == 1

    r2 = _post(client, monkeypatch, fake)  # precheck: 5 >= 1 -> blocked, adapter never touched
    assert r2.status_code == 402
    assert fake.call_count == 1


def test_get_budget_reflects_recorded_spend(client, monkeypatch, admin_headers):
    client.patch("/admin/budgets/data-science", json={"budget_cap_usd": 100.0}, headers=admin_headers)
    fake = _set_fixed_cost_per_call(client, cost_usd=4.5)
    _post(client, monkeypatch, fake)
    _post(client, monkeypatch, fake)

    resp = client.get("/admin/budgets/data-science", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["spend_usd"] == 9.0
    assert data["budget_cap_usd"] == 100.0
