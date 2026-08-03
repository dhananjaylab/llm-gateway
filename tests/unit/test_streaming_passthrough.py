"""
test_streaming_passthrough.py

Verifies: "SSE chunks arrive at the client in order and unmodified in
content; TTFT is captured; a mocked mid-stream client disconnect cancels
the upstream call" — per the Phase 1 test plan.
"""

from __future__ import annotations

import json
import logging

from app.api.v1_chat import _stream_response
from app.core.schema import ChatMessage, UnifiedChatRequest
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
    with caplog.at_level(logging.INFO, logger="gateway.v1_chat"):
        with client.stream(
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


async def test_client_disconnect_cancels_the_upstream_call():
    from app.core.config import TeamConfig, TeamPolicy

    adapter = _TrackingAdapter()
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

    received: list = []  # list[ServerSentEvent], the shape _stream_response yields
    async for event in _stream_response(
        adapter=adapter,
        payload={},
        request=unified_request,
        provider_model="gpt-5.4-served",
        http_request=fake_request,
        team=team,
    ):
        received.append(event)

    # Exactly one SSE event reached the "client" before disconnect was
    # detected and the loop broke.
    assert len(received) == 1
    # The upstream generator was cut off early — it did not run to
    # completion (5 content chunks + 1 terminal usage chunk = 6 total).
    assert adapter.yielded_count < 6
    # And its cleanup path (aclose()) ran, proving the upstream call was
    # actually cancelled rather than left dangling.
    assert adapter.stream_closed is True
