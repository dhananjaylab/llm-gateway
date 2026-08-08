"""
Authentication.

Per Document 05 (Backend Schema) SECURITY NOTE: team API keys are hashed
at rest and compared by hash, exactly like password storage — the raw key
is never stored, logged, or returned by any endpoint. Phase 1 hashed with
sha256 and compared against teams.yaml's `api_key_hash` field.

Phase 2 change: `resolve_team` now resolves against the Redis-backed
`TeamConfigStore` (app/core/team_store.py) instead of the static
in-memory `GatewayConfig` — this is what makes Admin API changes (Phase 2)
visible to the *next* request with no restart. The hashing contract itself
is unchanged from Phase 1.
"""

from __future__ import annotations

import hashlib

from fastapi import Header, HTTPException, Request, status

from app.core.config import TeamConfig


def hash_api_key(raw_api_key: str) -> str:
    return "sha256:" + hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()


async def resolve_team(
    request: Request,
    x_gateway_api_key: str = Header(..., alias="X-Gateway-API-Key"),
) -> TeamConfig:
    store = request.app.state.team_store
    team = await store.team_by_api_key_hash(hash_api_key(x_gateway_api_key))
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "type": "invalid_api_key",
                    "message": "Unknown or invalid X-Gateway-API-Key.",
                }
            },
        )
    return team


def enforce_model_allowed(team: TeamConfig, model_id: str) -> None:
    if model_id not in team.allowed_models:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "type": "model_not_allowed",
                    "message": (
                        f"Team '{team.team_id}' is not authorized to call model "
                        f"'{model_id}'. Allowed models: {team.allowed_models}."
                    ),
                }
            },
        )
