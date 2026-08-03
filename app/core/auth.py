"""
Authentication.

Per Document 05 (Backend Schema) SECURITY NOTE: team API keys are hashed
at rest and compared by hash, exactly like password storage — the raw key
is never stored, logged, or returned by any endpoint. Phase 1 hashes with
sha256 and compares against teams.yaml's `api_key_hash` field; this is the
same comparison Phase 2's Redis-backed lookup performs, just against a
YAML-sourced dict instead of `team_config:{team_id}` — the hashing contract
does not change between phases.

There is one linear auth path (per Document 03, App/Request Flow): client
presents X-Gateway-API-Key -> gateway resolves it to a TeamConfig -> the
request proceeds, or is rejected with 401 if the key is invalid/unknown.
The second check — is the requested model in this team's allow-list — is
deliberately *not* done here, since the model only becomes known once the
request body is parsed; see `enforce_model_allowed` in app/api/v1_chat.py.
"""

from __future__ import annotations

import hashlib

from fastapi import Header, HTTPException, status

from app.core.config import GatewayConfig, TeamConfig, get_gateway_config


def hash_api_key(raw_api_key: str) -> str:
    return "sha256:" + hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()


async def resolve_team(
    x_gateway_api_key: str = Header(..., alias="X-Gateway-API-Key"),
) -> TeamConfig:
    config: GatewayConfig = get_gateway_config()
    team = config.team_by_api_key_hash(hash_api_key(x_gateway_api_key))
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
