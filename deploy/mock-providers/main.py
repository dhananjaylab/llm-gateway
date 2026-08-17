"""
Mock upstream LLM providers for Phase 5 (docker-compose demo + integration
tests + the k6 load test).

Speaks the exact wire format each real adapter in app/providers/*.py
already parses -- OpenAI's Responses API, Anthropic's Messages API,
Ollama's native /api/chat, Gemini's generateContent -- so the gateway's
adapters run completely unmodified against this service; only the
*_BASE_URL env vars change (see docker-compose.yml's `gateway` service).
This is deliberate: a mock that diverges from the real wire shape would
let a broken adapter pass against the mock while failing against the
real provider, which defeats the entire point of having one.

Path layout mirrors each adapter's own base_url + path-suffix
construction, so a base_url of e.g. http://mock-providers:9000/openai
produces exactly the URL the real OpenAIAdapter already builds
(base_url + "/v1/responses"):

    /openai/v1/responses                              (OpenAIAdapter)
    /anthropic/v1/messages                             (AnthropicAdapter)
    /ollama/api/chat                                   (OllamaAdapter)
    /gemini/v1beta/models/{model}:generateContent       (GeminiAdapter)

Chaos injection (chaos.py) is checked once per request, up front, before
any response (streaming or not) is built. This deliberately does NOT
support mid-stream failures: every real adapter's stream() method already
checks the initial HTTP response status before it starts reading
SSE/NDJSON lines (`if resp.status_code >= 400: ... raise`), so a
chaos-triggered failure is returned as a plain JSON error response
instead of ever opening a stream body -- exactly the pre-first-chunk
failure path app/resilience/fallback.py's stream_with_fallback() is
scoped to handle. Faking a mid-stream disconnect would be testing a
code path (Phase 1/2's mid-stream `event: error` frame) Phase 5 isn't
asked to add new coverage for.
"""

from __future__ import annotations

import json
import time
import uuid

from chaos import ChaosController, ChaosInjectedError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="LLM Gateway Mock Providers", version="1.0.0")
chaos = ChaosController()

# Deterministic, small usage numbers -- mirrors tests/unit/conftest.py's
# FakeAdapter convention (input=10) so a reader cross-referencing the two
# test doubles doesn't have to hold two different numbers in their head;
# output bumped slightly (5 -> 5, unchanged) to keep cost-calc math simple.
_MOCK_INPUT_TOKENS = 10
_MOCK_OUTPUT_TOKENS = 5
_MOCK_TEXT = "This is a mock response from the Phase 5 test double."
_MOCK_CHUNKS = ["This ", "is ", "a ", "mock ", "response."]


@app.exception_handler(ChaosInjectedError)
async def _chaos_error_handler(request: Request, exc: ChaosInjectedError) -> JSONResponse:
    """
    Deliberately NOT a raised fastapi.HTTPException: FastAPI's default
    handler wraps HTTPException.detail as {"detail": ...} in the response
    body, but every real adapter's error parser expects the UPSTREAM
    PROVIDER's own top-level shape ({"error": {...}} for
    openai/anthropic/gemini, {"error": "<string>"} for ollama) with no
    "detail" wrapper -- that wrapper is this gateway's own outward-facing
    convention (see app/core/auth.py etc.), not any real provider's. A
    plain HTTPException here would make every adapter's
    `json.loads(body).get("error", ...)` silently find nothing and fall
    through to the raw-bytes fallback, masking the chaos body entirely.
    """
    provider = request.url.path.strip("/").split("/", 1)[0]
    if provider == "ollama":
        body: dict = {"error": f"mock chaos injection ({exc.error_type})"}
    else:
        body = {"error": {"message": f"mock chaos injection ({exc.error_type})", "type": exc.error_type}}
    return JSONResponse(status_code=exc.status_code, content=body)


# -- chaos control plane ------------------------------------------------------


class ChaosConfigIn(BaseModel):
    provider: str
    model: str = "*"
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    status_code: int = 503  # see chaos.py's module docstring for why 503, not literal 500
    error_type: str = "mock_chaos_injected"


@app.post("/_chaos/config")
async def set_chaos(cfg: ChaosConfigIn):
    rule = chaos.set_rule(
        provider=cfg.provider,
        model=cfg.model,
        error_rate=cfg.error_rate,
        latency_ms=cfg.latency_ms,
        status_code=cfg.status_code,
        error_type=cfg.error_type,
    )
    return {
        "provider": cfg.provider,
        "model": cfg.model,
        **{k: v for k, v in rule.__dict__.items() if k != "set_at"},
    }


@app.get("/_chaos/config")
async def get_chaos():
    return chaos.snapshot()


@app.post("/_chaos/reset")
async def reset_chaos(provider: str | None = None, model: str = "*"):
    chaos.clear(provider=provider, model=model)
    return {"cleared": provider or "all"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# -- OpenAI: POST /openai/v1/responses ----------------------------------------


@app.post("/openai/v1/responses")
async def openai_responses(request: Request):
    payload = await request.json()
    model = payload.get("model", "mock-model")
    await chaos.apply(provider="openai", model=model)

    if payload.get("stream"):
        return StreamingResponse(_openai_stream(model), media_type="text/event-stream")

    return {
        "id": f"resp_mock_{uuid.uuid4().hex[:16]}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output_text": _MOCK_TEXT,
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": _MOCK_TEXT}],
            }
        ],
        "usage": {
            "input_tokens": _MOCK_INPUT_TOKENS,
            "output_tokens": _MOCK_OUTPUT_TOKENS,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }


async def _openai_stream(model: str):
    for chunk in _MOCK_CHUNKS:
        yield f"event: response.output_text.delta\ndata: {json.dumps({'delta': chunk})}\n\n"
    completed = {
        "response": {
            "model": model,
            "usage": {"input_tokens": _MOCK_INPUT_TOKENS, "output_tokens": _MOCK_OUTPUT_TOKENS},
        }
    }
    yield f"event: response.completed\ndata: {json.dumps(completed)}\n\n"


# -- Anthropic: POST /anthropic/v1/messages -----------------------------------


@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    payload = await request.json()
    model = payload.get("model", "mock-model")
    await chaos.apply(provider="anthropic", model=model)

    if payload.get("stream"):
        return StreamingResponse(_anthropic_stream(model), media_type="text/event-stream")

    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": _MOCK_TEXT}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": _MOCK_INPUT_TOKENS,
            "output_tokens": _MOCK_OUTPUT_TOKENS,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


async def _anthropic_stream(model: str):
    message_id = f"msg_mock_{uuid.uuid4().hex[:16]}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse(
        "message_start",
        {"message": {"id": message_id, "model": model, "usage": {"input_tokens": _MOCK_INPUT_TOKENS}}},
    )
    for chunk in _MOCK_CHUNKS:
        yield sse("content_block_delta", {"delta": {"type": "text_delta", "text": chunk}})
    yield sse(
        "message_delta",
        {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": _MOCK_OUTPUT_TOKENS}},
    )
    yield sse("message_stop", {})


# -- Ollama: POST /ollama/api/chat --------------------------------------------


@app.post("/ollama/api/chat")
async def ollama_chat(request: Request):
    payload = await request.json()
    model = payload.get("model", "mock-model")
    await chaos.apply(provider="ollama", model=model)

    if payload.get("stream"):
        return StreamingResponse(_ollama_stream(model), media_type="application/x-ndjson")

    return {
        "model": model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": _MOCK_TEXT},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": _MOCK_INPUT_TOKENS,
        "eval_count": _MOCK_OUTPUT_TOKENS,
    }


async def _ollama_stream(model: str):
    for chunk in _MOCK_CHUNKS:
        line = {"model": model, "message": {"role": "assistant", "content": chunk}, "done": False}
        yield json.dumps(line) + "\n"
    final = {
        "model": model,
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "prompt_eval_count": _MOCK_INPUT_TOKENS,
        "eval_count": _MOCK_OUTPUT_TOKENS,
    }
    yield json.dumps(final) + "\n"


# -- Gemini: POST /gemini/v1beta/models/{model}:generateContent --------------


@app.post("/gemini/v1beta/models/{model_and_action}")
async def gemini_generate_content(model_and_action: str, request: Request):
    # Gemini addresses the model via the URL path, suffixed with the
    # action ("gemini-3.6-flash:generateContent") rather than a body
    # field -- split it back apart rather than requiring FastAPI to
    # route on a literal colon in the path.
    model = model_and_action.split(":", 1)[0]
    await chaos.apply(provider="gemini", model=model)

    return {
        "candidates": [
            {"content": {"parts": [{"text": _MOCK_TEXT}], "role": "model"}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            "promptTokenCount": _MOCK_INPUT_TOKENS,
            "candidatesTokenCount": _MOCK_OUTPUT_TOKENS,
            "totalTokenCount": _MOCK_INPUT_TOKENS + _MOCK_OUTPUT_TOKENS,
        },
    }
