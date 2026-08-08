"""
test_priority_queue.py

Verifies the Phase 2 test plan: "Under simulated near-capacity load,
Batch-priority requests queue while Normal/High requests continue to be
served."

Scope reminder (see app/ratelimit/priority_queue.py's module docstring):
this is queueing behavior, not a reserved-capacity split. All priorities
share one team-level bucket; Batch gets a bounded wait-and-retry on
denial, Normal/High get an immediate allow/deny with no wait. These tests
exercise exactly that contract.

Timing: BATCH_QUEUE_MAX_WAIT_SECONDS=1.5 / POLL_INTERVAL=0.05 are set for
the whole test session by conftest.py's autouse env fixture — small
enough to keep the suite fast, large enough to give the "capacity frees
up mid-wait" tests comfortable margin against scheduler jitter.
"""

from __future__ import annotations

import asyncio

from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client


def _body(priority: str) -> dict:
    return {
        "model": "openai:gpt-5.4",
        "messages": [{"role": "user", "content": "hi"}],
        "priority": priority,
    }


async def test_batch_request_queues_and_succeeds_once_capacity_frees_up(app, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

    async with running_app_client(app) as client:
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 1, "tpm_cap": 1_000_000}
        )

        # Exhaust the single RPM token.
        first = await client.post("/v1/chat/completions", json=_body("normal"), headers=headers)
        assert first.status_code == 200

        async def restore_capacity_shortly():
            await asyncio.sleep(0.4)  # well inside the 1.5s max wait
            await app.state.redis.hset("rl:data-science:rpm", mapping={"tokens": 5})

        batch_response, queue_depth_mid_wait, _ = await asyncio.gather(
            client.post("/v1/chat/completions", json=_body("batch"), headers=headers),
            _sample_queue_depth_after(app, delay=0.15),
            restore_capacity_shortly(),
        )

        assert batch_response.status_code == 200, batch_response.text
        assert queue_depth_mid_wait >= 1, "the batch request should have been sitting in the queue"


async def _sample_queue_depth_after(app, *, delay: float) -> int:
    await asyncio.sleep(delay)
    return await app.state.batch_queue.queue_depth("data-science")


async def test_normal_priority_is_denied_immediately_not_queued(app, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

    async with running_app_client(app) as client:
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 1, "tpm_cap": 1_000_000}
        )
        first = await client.post("/v1/chat/completions", json=_body("normal"), headers=headers)
        assert first.status_code == 200

        loop = asyncio.get_event_loop()
        start = loop.time()
        second = await client.post("/v1/chat/completions", json=_body("normal"), headers=headers)
        elapsed = loop.time() - start

        assert second.status_code == 429
        # "Continues immediately" — must not have waited anywhere near the
        # batch queue's max-wait window.
        assert elapsed < 0.5

        # And it must never have touched the batch queue at all.
        depth = await app.state.batch_queue.queue_depth("data-science")
        assert depth == 0


async def test_batch_request_still_gets_429_if_capacity_never_frees_up(app, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

    async with running_app_client(app) as client:
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 1, "tpm_cap": 1_000_000}
        )
        first = await client.post("/v1/chat/completions", json=_body("normal"), headers=headers)
        assert first.status_code == 200

        # No refill this time — the queued batch request must time out and
        # surface the original 429 rather than hanging forever.
        second = await client.post("/v1/chat/completions", json=_body("batch"), headers=headers)
        assert second.status_code == 429
        assert second.json()["detail"]["error"]["type"] == "rate_limit_exceeded"

        # And the ticket must have been removed from the queue on exit
        # (no leaked entries after a timed-out wait).
        depth = await app.state.batch_queue.queue_depth("data-science")
        assert depth == 0


async def test_batch_request_does_not_queue_at_all_when_capacity_is_available(app, monkeypatch):
    """Sanity check: queueing is only for the denied case — a Batch
    request that would be admitted anyway must not pay any queueing
    latency at all."""
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

    async with running_app_client(app) as client:
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 100, "tpm_cap": 1_000_000}
        )
        loop = asyncio.get_event_loop()
        start = loop.time()
        resp = await client.post("/v1/chat/completions", json=_body("batch"), headers=headers)
        elapsed = loop.time() - start

        assert resp.status_code == 200
        assert elapsed < 0.3
