"""
Cache key derivation (Phase 9b, docs/PHASE9B_KICKOFF_SCOPING.md Sec 3.2/4.1).

Three related-but-distinct strings, each with its own job:

- `scope()` -- the ISOLATION boundary. Physically separates one Redis key
  family from another so that a tenant, a model/tier, or a policy can never
  read another's cache entries by construction (Sec 4.1: "physical namespace
  separation, not filter-only"). Scoped by the REQUESTED model/tier string,
  not whichever provider ends up serving it -- the cache check runs before
  fallback routing resolves a chain, so the served provider isn't known yet,
  and scoping this way also means Phase 9a's tier hot-reload can repoint a
  chain without invalidating the scope (bounded staleness via CACHE_TTL_SECONDS
  instead, a documented tradeoff -- see the kickoff doc).
- `policy_fingerprint()` -- folds into `scope()`. A policy change (PII
  redaction toggled, system prompt changed) must produce a DIFFERENT scope,
  never silently serve an answer generated under the old policy.
- `request_fingerprint()` -- the EXACT-MATCH key within a scope. Every field
  that could change the answer goes in, deliberately including generation
  parameters (Sec 4.5: "parameters must match exactly", a semantic hit never
  substitutes for a parameter mismatch, only for prompt-text distance).

None of this module touches Redis or does I/O -- pure functions only, which
is also why it's first in the build order (docs/PHASE9B_KICKOFF_SCOPING.md
Sec 11): the cheapest thing to get fully unit-tested before anything that
needs fakeredis exists at all.
"""

from __future__ import annotations

import hashlib
import json

from app.core.config import TeamConfig
from app.core.schema import UnifiedChatRequest

_SCOPE_HASH_LEN = 20
_POLICY_FINGERPRINT_LEN = 12


def policy_fingerprint(team: TeamConfig) -> str:
    """Short hash of the policy fields that affect what the cached response
    would legitimately contain. Anything not here (e.g. rpm_cap) doesn't
    belong -- rate limits don't change what a cached ANSWER should be."""
    raw = json.dumps(
        {
            "system_prompt_prefix": team.policy.system_prompt_prefix,
            "pii_redaction": team.policy.pii_redaction,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_POLICY_FINGERPRINT_LEN]


def scope(*, team: TeamConfig, requested_model: str) -> str:
    """The Redis key-family boundary -- see module docstring. Deterministic
    and stable across processes (no randomness, no wall-clock component)."""
    raw = f"{team.team_id}|{requested_model}|{policy_fingerprint(team)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SCOPE_HASH_LEN]


def canonical_text(request: UnifiedChatRequest) -> str:
    """The text the embedder actually sees -- system prompt plus every
    message's content, in order, newline-joined. Deliberately NOT including
    tool definitions or generation parameters: this function's whole job is
    producing the string whose *semantic meaning* the embedding captures;
    parameters are compared separately and exactly (Sec 4.5), never folded
    into the embedding itself."""
    parts: list[str] = []
    if request.system:
        parts.append(request.system)
    parts.extend(m.content for m in request.messages if m.content)
    return "\n".join(parts)


def request_fingerprint(request: UnifiedChatRequest) -> str:
    """Exact-match key: sha256 of every field that could change the answer.
    Order-independent by construction (json.dumps(..., sort_keys=True)) so
    two structurally-identical requests built by different client code still
    collide correctly."""
    payload = {
        "model": request.model,
        "system": request.system,
        "messages": [
            {"role": m.role, "content": m.content} for m in request.messages
        ],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "stop": request.stop,
        "response_format": (
            request.response_format.model_dump() if request.response_format else None
        ),
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def params_match(request: UnifiedChatRequest, stored: dict) -> bool:
    """Sec 4.5 / Sec 9 point 3: a semantic hit substitutes for prompt-text
    distance ONLY -- generation parameters must match the stored entry's
    request exactly, or the candidate is not eligible no matter how close
    the embedding similarity scored. `stored` is the same shape
    `request_fingerprint`'s payload uses, read back from the cache entry."""
    return (
        request.max_tokens == stored.get("max_tokens")
        and request.temperature == stored.get("temperature")
        and request.top_p == stored.get("top_p")
        and request.stop == stored.get("stop")
        and (request.response_format.model_dump() if request.response_format else None)
        == stored.get("response_format")
    )
