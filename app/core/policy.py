"""
Request enrichment: system-prompt injection and PII redaction.

Unchanged from Phase 1 — Phase 2 does not touch policy application, only
what happens before/after it in the pipeline (rate limit + budget now
wrap it with real enforcement instead of stubs).
"""

from __future__ import annotations

import re

from app.core.config import TeamConfig
from app.core.schema import ChatMessage, UnifiedChatRequest

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")


def redact_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _SSN_RE.sub("[REDACTED_SSN]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return text


def apply_policy(request: UnifiedChatRequest, team: TeamConfig) -> UnifiedChatRequest:
    """Return a new, policy-enriched request. Never mutates `request`."""
    enriched = request.model_copy(deep=True)

    if team.policy.pii_redaction:
        enriched.messages = [
            ChatMessage(role=m.role, content=redact_pii(m.content)) for m in enriched.messages
        ]

    if team.policy.system_prompt_prefix:
        if enriched.system:
            enriched.system = f"{team.policy.system_prompt_prefix}\n\n{enriched.system}"
        else:
            enriched.system = team.policy.system_prompt_prefix

    return enriched
