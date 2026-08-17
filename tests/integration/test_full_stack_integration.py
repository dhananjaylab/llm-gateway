"""
test_full_stack_integration.py

Document 06 Phase 5 test plan: rate limiting under concurrency, budget
caps at the right threshold, fallback activation on simulated failures,
circuit-breaker open/close correctness, and streaming integrity -- run
against the real docker-compose stack over real HTTP, not the in-process
fakeredis app the unit suite uses.

Every test here is skipped (not failed) when the stack isn't reachable --
see conftest.py's `requires_live_stack`. Run `docker compose up -d`
first, or see the README's "Run the integration suite" section.

Each test PATCHes the limits/budget it needs at the top rather than
relying on a clean starting state -- tests share one long-lived
container's Redis, so idempotent setup (not teardown) is what keeps them
independent of run order, matching the same pattern the unit suite's
Admin API tests already use.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from tests.integration.conftest import GATEWAY_BASE_URL, requires_live_stack

TEAM_HEADERS = {"X-Gateway-API-Key": "sk-gw-datascience-demo-001"}
PRODUCT_ENG_HEADERS = {"X-Gateway-API-Key": "sk-gw-producteng-demo-002"}
BATCH_DEVS_HEADERS = {"X-Gateway-API-Key": "sk-gw-batchdevs-demo-003"}
ADMIN_HEADERS = {"X-Gateway-Admin-Key": os.environ.get("GATEWAY_ADMIN_KEY", "dev-admin-key-change-me")}

pytestmark = requires_live_stack


def _parse_sse(raw_text: str) -> list[dict]:
    events = []
    for block in raw_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


async def _generous_limits(client: httpx.AsyncClient) -> None:
    """Most tests below aren't about rate/budget enforcement -- reset both
    to generous values first so an earlier test's tight cap doesn't leak
    into an unrelated one."""
    await client.patch(
        "/admin/limits/data-science", json={"rpm_cap": 1000, "tpm_cap": 10_000_000}, headers=ADMIN_HEADERS
    )
    await client.patch(
        "/admin/budgets/data-science", json={"budget_cap_usd": 1000.0}, headers=ADMIN_HEADERS
    )


async def _wait_for_full_rpm_bucket(client: httpx.AsyncClient, *, team_id: str, rpm_cap: int) -> None:
    """
    A concurrency-burst test needs the bucket at (or near) full capacity
    before it fires, but raising rpm_cap via PATCH only changes the
    ceiling and refill rate -- it does NOT teleport the bucket's current
    token count back up (the Lua script's refill math is `min(capacity,
    tokens + delta * refill_rate)`, which is correct production behavior,
    just inconvenient for a test that reruns against the same long-lived
    container instead of a fresh fakeredis per test). Compute exactly how
    long the refill needs and wait that long, rather than guessing or
    assuming a previous run never touched this bucket.
    """
    limits = (await client.get(f"/admin/limits/{team_id}", headers=ADMIN_HEADERS)).json()
    remaining = limits["remaining_rpm"]
    if remaining >= rpm_cap:
        return
    refill_per_second = rpm_cap / 60.0
    wait_seconds = (rpm_cap - remaining) / refill_per_second if refill_per_second > 0 else 0
    await asyncio.sleep(min(wait_seconds, 65.0) + 0.5)


async def test_rate_limit_enforces_exactly_the_configured_rpm_under_concurrency():
    """Uses batch-devs (ollama:llama3.2 only, $0 pricing) rather than
    data-science -- this test deliberately drains a bucket to zero, and
    running it against the same team the budget/fallback tests below use
    would leave their RPM tokens starved by leftover state. Isolating it
    to its own team keeps that drain from leaking into unrelated tests;
    `_wait_for_full_rpm_bucket` (above) is what keeps THIS test itself
    reproducible across repeated runs against the same live container."""
    async with httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=90.0) as client:
        patch = await client.patch(
            "/admin/limits/batch-devs",
            json={"rpm_cap": 10, "tpm_cap": 10_000_000},
            headers=ADMIN_HEADERS,
        )
        assert patch.status_code == 200
        await _wait_for_full_rpm_bucket(client, team_id="batch-devs", rpm_cap=10)

        body = {"model": "ollama:llama3.2", "messages": [{"role": "user", "content": "hi"}]}
        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=body, headers=BATCH_DEVS_HEADERS) for _ in range(50)]
        )
        statuses = [r.status_code for r in responses]
        assert statuses.count(200) == 10, f"expected exactly 10 successes, got: {statuses}"
        assert statuses.count(429) == 40


async def test_budget_cap_blocks_the_request_after_the_one_that_crosses_it():
    """Uses product-eng, for the same team-isolation reason the
    concurrency test above uses batch-devs -- and the dated Claude Haiku
    snapshot already in its allow-list (config/teams.yaml), since
    ollama:llama3.2 is $0-priced (config/pricing.yaml) and could never
    cross a budget cap no matter how small.

    Reads CURRENT spend before picking a cap, rather than assuming spend
    starts at 0 -- this container's Redis persists across repeated runs
    of this same test (unlike the unit suite's fresh fakeredis per test),
    so a hardcoded tiny cap like 1e-7 eventually sits BELOW leftover
    spend from an earlier run and makes even the first call 402
    immediately. Setting the cap to "current spend + a hair" is what
    keeps this reproducible regardless of run history."""
    async with httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0) as client:
        await client.patch(
            "/admin/limits/product-eng", json={"rpm_cap": 1000, "tpm_cap": 10_000_000}, headers=ADMIN_HEADERS
        )
        await client.patch(
            "/admin/budgets/product-eng", json={"budget_cap_usd": 200.0}, headers=ADMIN_HEADERS
        )
        current = (await client.get("/admin/budgets/product-eng", headers=ADMIN_HEADERS)).json()
        await client.patch(
            "/admin/budgets/product-eng",
            json={"budget_cap_usd": current["spend_usd"] + 0.00001},
            headers=ADMIN_HEADERS,
        )

        body = {
            "model": "anthropic:claude-haiku-4-5-20251001",
            "messages": [{"role": "user", "content": "hi"}],
        }
        first = await client.post("/v1/chat/completions", json=body, headers=PRODUCT_ENG_HEADERS)
        assert first.status_code == 200, first.text

        second = await client.post("/v1/chat/completions", json=body, headers=PRODUCT_ENG_HEADERS)
        assert second.status_code == 402
        assert second.json()["detail"]["error"]["type"] == "budget_exceeded"

        # Restore a real cap so a subsequent manual demo against this
        # same long-lived container isn't left permanently budget-locked.
        await client.patch(
            "/admin/budgets/product-eng", json={"budget_cap_usd": 200.0}, headers=ADMIN_HEADERS
        )


async def test_fallback_activates_on_a_live_simulated_provider_outage(mock_providers_client):
    async with httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0) as client:
        await _generous_limits(client)

        body = {"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]}
        healthy = await client.post("/v1/chat/completions", json=body, headers=TEAM_HEADERS)
        assert healthy.status_code == 200
        assert healthy.headers["x-gateway-served-model"].startswith("openai:")

        mock_providers_client.post("/_chaos/config", json={"provider": "openai", "error_rate": 1.0})
        try:
            failed_over = await client.post("/v1/chat/completions", json=body, headers=TEAM_HEADERS)
            assert failed_over.status_code == 200, failed_over.text
            assert failed_over.headers["x-gateway-served-model"].startswith("anthropic:"), (
                "client must still get a clean 200 from the next chain link -- "
                "Document 03 Journey B's whole point"
            )
        finally:
            mock_providers_client.post("/_chaos/reset")


async def test_circuit_breaker_opens_after_sustained_real_failures(mock_providers_client):
    async with httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0) as client:
        await _generous_limits(client)

        mock_providers_client.post("/_chaos/config", json={"provider": "gemini", "error_rate": 1.0})
        try:
            # A literal single-link model id isolates one circuit, same
            # technique test_fallback_chain.py's unit-level equivalent
            # uses. CIRCUIT_BREAKER_FAILURE_THRESHOLD default is 5.
            body = {"model": "gemini:gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]}
            for _ in range(5):
                resp = await client.post("/v1/chat/completions", json=body, headers=TEAM_HEADERS)
                assert resp.status_code == 503

            circuits = (await client.get("/admin/circuits", headers=ADMIN_HEADERS)).json()
            gemini_status = next(
                c for c in circuits if c["provider"] == "gemini" and c["model"] == "gemini-3.6-flash"
            )
            assert gemini_status["state"] == "open"
            # Full Open -> Half-Open -> Closed recovery timing is already
            # proven deterministically at the unit level (an injectable
            # clock, tests/unit/test_circuit_breaker.py) -- this test's
            # job is proving the OPEN transition happens for real, end to
            # end, not re-waiting out a real 60s cooldown here too.
        finally:
            mock_providers_client.post("/_chaos/reset")


async def test_streaming_response_arrives_in_order_and_intact():
    async with httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0) as client:
        await _generous_limits(client)

        body = {
            "model": "ollama:llama3.2",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        async with client.stream(
            "POST", "/v1/chat/completions", json=body, headers=TEAM_HEADERS
        ) as resp:
            assert resp.status_code == 200
            raw_text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_sse(raw_text)
    deltas = [e["delta"] for e in events if e.get("delta")]
    assert "".join(deltas) == "This is a mock response."
    assert events[-1]["finish_reason"] == "stop"
    assert events[-1]["usage"]["output_tokens"] == 5
