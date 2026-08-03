"""
test_policy_injection.py

Verifies: "System prompt prefix and PII redaction apply only for teams
with the policy enabled, and never mutate the original client payload in
logs" — per the Phase 1 test plan.
"""

from __future__ import annotations

from app.core.config import TeamConfig, TeamPolicy
from app.core.policy import apply_policy, redact_pii
from app.core.schema import ChatMessage, UnifiedChatRequest


def _team(**policy_kwargs) -> TeamConfig:
    return TeamConfig(
        team_id="t1",
        api_key_hash="sha256:irrelevant",
        allowed_models=["openai:gpt-5.4"],
        policy=TeamPolicy(**policy_kwargs),
    )


def _request(content: str, system: str | None = None) -> UnifiedChatRequest:
    return UnifiedChatRequest(
        model="openai:gpt-5.4",
        system=system,
        messages=[ChatMessage(role="user", content=content)],
    )


def test_redact_pii_masks_email_ssn_and_phone():
    text = "Reach me at jane.doe@example.com, SSN 456-78-9012, phone 555-019-9999."
    redacted = redact_pii(text)
    assert "jane.doe@example.com" not in redacted
    assert "456-78-9012" not in redacted
    assert "555-019-9999" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_SSN]" in redacted
    assert "[REDACTED_PHONE]" in redacted


def test_pii_redaction_applies_when_team_has_it_enabled():
    team = _team(pii_redaction=True)
    request = _request("email me at a@b.com please")
    enriched = apply_policy(request, team)
    assert "a@b.com" not in enriched.messages[0].content
    assert "[REDACTED_EMAIL]" in enriched.messages[0].content


def test_pii_redaction_does_not_apply_when_team_has_it_disabled():
    team = _team(pii_redaction=False)
    request = _request("email me at a@b.com please")
    enriched = apply_policy(request, team)
    assert enriched.messages[0].content == "email me at a@b.com please"


def test_system_prompt_prefix_applies_when_configured():
    team = _team(system_prompt_prefix="You are ACME's assistant.")
    request = _request("hello", system=None)
    enriched = apply_policy(request, team)
    assert enriched.system == "You are ACME's assistant."


def test_system_prompt_prefix_is_prepended_to_existing_system():
    team = _team(system_prompt_prefix="ORG POLICY:")
    request = _request("hello", system="Be concise.")
    enriched = apply_policy(request, team)
    assert enriched.system == "ORG POLICY:\n\nBe concise."


def test_no_system_prompt_prefix_leaves_system_untouched():
    team = _team(system_prompt_prefix=None)
    request = _request("hello", system="Be concise.")
    enriched = apply_policy(request, team)
    assert enriched.system == "Be concise."


def test_apply_policy_never_mutates_the_original_request():
    team = _team(pii_redaction=True, system_prompt_prefix="PREFIX")
    original = _request("contact a@b.com", system="original system")
    original_content_before = original.messages[0].content
    original_system_before = original.system

    enriched = apply_policy(original, team)

    assert enriched is not original
    assert original.messages[0].content == original_content_before
    assert original.system == original_system_before
    # And the enriched copy actually did change, proving this isn't a
    # trivial pass — the original and the enriched copy now differ.
    assert enriched.messages[0].content != original.messages[0].content
    assert enriched.system != original.system
