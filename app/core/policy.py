"""
Request enrichment: system-prompt injection and PII redaction.

Unchanged from Phase 1 through Phase 7 — only what happens before/after
it in the pipeline changed across those phases.

Phase 8 fix: redaction used to rebuild every message as
`ChatMessage(role=m.role, content=redact_pii(m.content))`, unconditionally
dropping any other field. That was silently correct through Phase 7
(ChatMessage had no other fields), but Phase 8 added `tool_calls`/
`tool_call_id` — rebuilding without them means a team with PII redaction
enabled would have every tool-role message rebuilt with `tool_call_id`
missing, which fails ChatMessage's own model_validator (a tool-role
message must set tool_call_id), and every tool-calling assistant turn
rebuilt with `tool_calls` dropped, corrupting the conversation. Redaction
now only ever touches `content`, preserving every other field verbatim —
`tool_calls`/`tool_call_id` are structured, model/tool-generated data,
not free-form user text, so they are deliberately NOT scanned for PII
this phase (a tool's arguments could in principle echo a redactable
string the user typed, e.g. an email passed as a lookup key — flagged as
a known, documented limitation rather than silently assumed safe).
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
            ChatMessage(
                role=m.role,
                content=redact_pii(m.content),
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
            )
            for m in enriched.messages
        ]

    if team.policy.system_prompt_prefix:
        if enriched.system:
            enriched.system = f"{team.policy.system_prompt_prefix}\n\n{enriched.system}"
        else:
            enriched.system = team.policy.system_prompt_prefix

    return enriched
