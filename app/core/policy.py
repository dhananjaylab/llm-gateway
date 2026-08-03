"""
Request enrichment: system-prompt injection and PII redaction.

Both are configurable per team via teams.yaml (`policy.system_prompt_prefix`,
`policy.pii_redaction`), never hardcoded — this is what "centralizes policy
enforcement without requiring every team to implement it themselves" means
in the original project brief.

`apply_policy` returns a *new* UnifiedChatRequest and never mutates the one
it was given: the TRD's Phase 1 test plan explicitly checks that policy
application "never mutate[s] the original client payload in logs," which
only holds if callers keep a reference to the pre-policy request for
logging/audit before this function ever touches it.

v1 PII patterns are intentionally simple (regex, not an NLP model) per the
PRD's "Out of scope" section — matches the reference implementation in
enterprise_llm_gateway_architect.html.
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
