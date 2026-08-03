"""
FastAPI app factory.

GET /healthz and /readyz are liveness/readiness for the gateway *process*
itself (container orchestration probes) — not provider health, which is a
Phase 3 concept (app/resilience/health.py) exposed on the Operations
Grafana dashboard, not here.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.v1_chat import router as v1_chat_router

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        description=(
            "Multi-provider LLM API gateway: unified schema, rate limiting, "
            "fallback routing, and observability. Phase 1: unified proxy layer."
        ),
    )

    app.include_router(v1_chat_router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        # Phase 1: the process is ready as soon as it can serve traffic —
        # there is no Redis/provider dependency to check readiness against
        # yet. Phase 2 adds a Redis ping here.
        return {"status": "ready"}

    return app


app = create_app()
