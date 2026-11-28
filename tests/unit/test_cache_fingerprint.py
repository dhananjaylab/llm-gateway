"""
test_cache_fingerprint.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md's fingerprint/scope module: pure
functions, no I/O -- deterministic, order-independent where they should be,
and sensitive to every field that affects the answer.
"""

from __future__ import annotations

from app.caching.fingerprint import (
    canonical_text,
    params_match,
    policy_fingerprint,
    request_fingerprint,
    scope,
)
from app.core.config import TeamConfig, TeamPolicy
from app.core.schema import ChatMessage, ResponseFormat, UnifiedChatRequest


def _team(**policy_kwargs) -> TeamConfig:
    return TeamConfig(
        team_id="data-science",
        api_key_hash="sha256:irrelevant",
        allowed_models=["openai:gpt-5.4"],
        policy=TeamPolicy(**policy_kwargs),
    )


def _request(**overrides) -> UnifiedChatRequest:
    defaults = dict(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    defaults.update(overrides)
    return UnifiedChatRequest(**defaults)


# -- policy_fingerprint --------------------------------------------------


def test_policy_fingerprint_is_deterministic():
    team = _team(pii_redaction=True, system_prompt_prefix="be nice")
    assert policy_fingerprint(team) == policy_fingerprint(team)


def test_policy_fingerprint_changes_when_pii_redaction_toggles():
    a = policy_fingerprint(_team(pii_redaction=True))
    b = policy_fingerprint(_team(pii_redaction=False))
    assert a != b


def test_policy_fingerprint_changes_when_system_prompt_prefix_changes():
    a = policy_fingerprint(_team(system_prompt_prefix="A"))
    b = policy_fingerprint(_team(system_prompt_prefix="B"))
    assert a != b


def test_policy_fingerprint_unaffected_by_fields_that_dont_affect_answers():
    """rpm_cap/tpm_cap/budget_cap_usd don't change what a cached ANSWER
    should be -- only policy fields belong in this fingerprint."""
    team_a = _team(pii_redaction=True)
    team_b = _team(pii_redaction=True)
    team_b.rpm_cap = 999999
    team_b.budget_cap_usd = 1.0
    assert policy_fingerprint(team_a) == policy_fingerprint(team_b)


# -- scope -----------------------------------------------------------------


def test_scope_is_deterministic_and_short():
    team = _team()
    s1 = scope(team=team, requested_model="tier-1-reasoning")
    s2 = scope(team=team, requested_model="tier-1-reasoning")
    assert s1 == s2
    assert len(s1) == 20


def test_scope_differs_across_teams_even_with_identical_policy():
    """The isolation-critical property (kickoff Sec 4.1 / Sec 9): two teams
    with byte-identical TeamPolicy must never land in the same scope."""
    team_a = TeamConfig(team_id="team-a", api_key_hash="x", allowed_models=[])
    team_b = TeamConfig(team_id="team-b", api_key_hash="x", allowed_models=[])
    assert scope(team=team_a, requested_model="openai:gpt-5.4") != scope(
        team=team_b, requested_model="openai:gpt-5.4"
    )


def test_scope_differs_across_requested_models_for_the_same_team():
    team = _team()
    assert scope(team=team, requested_model="openai:gpt-5.4") != scope(
        team=team, requested_model="anthropic:claude-sonnet-5"
    )


def test_scope_differs_across_policy_even_for_the_same_team_and_model():
    """The same team, same model, different policy (e.g. pii_redaction just
    got turned on) must land in a different scope -- never silently serve an
    answer generated under the old policy."""
    team_off = _team(pii_redaction=False)
    team_off.team_id = "same-team"
    team_on = _team(pii_redaction=True)
    team_on.team_id = "same-team"
    assert scope(team=team_off, requested_model="openai:gpt-5.4") != scope(
        team=team_on, requested_model="openai:gpt-5.4"
    )


# -- canonical_text ----------------------------------------------------------


def test_canonical_text_includes_system_and_message_content_in_order():
    req = _request(
        system="Be concise.",
        messages=[ChatMessage(role="user", content="first"), ChatMessage(role="assistant", content="second")],
    )
    text = canonical_text(req)
    assert text == "Be concise.\nfirst\nsecond"


def test_canonical_text_omits_system_when_absent():
    req = _request(system=None, messages=[ChatMessage(role="user", content="hi")])
    assert canonical_text(req) == "hi"


# -- request_fingerprint ------------------------------------------------


def test_request_fingerprint_is_deterministic():
    req = _request()
    assert request_fingerprint(req) == request_fingerprint(req)


def test_request_fingerprint_changes_when_message_content_changes():
    a = request_fingerprint(_request(messages=[ChatMessage(role="user", content="hi")]))
    b = request_fingerprint(_request(messages=[ChatMessage(role="user", content="bye")]))
    assert a != b


def test_request_fingerprint_changes_when_max_tokens_changes():
    a = request_fingerprint(_request(max_tokens=100))
    b = request_fingerprint(_request(max_tokens=200))
    assert a != b


def test_request_fingerprint_changes_when_temperature_changes():
    a = request_fingerprint(_request(temperature=0.0))
    b = request_fingerprint(_request(temperature=0.2))
    assert a != b


def test_request_fingerprint_changes_when_response_format_changes():
    a = request_fingerprint(_request())
    schema = {"name": "x", "schema": {"type": "object"}}
    b = request_fingerprint(_request(response_format=ResponseFormat(type="json_schema", json_schema=schema)))
    assert a != b


def test_request_fingerprint_is_the_same_across_two_independently_built_identical_requests():
    req_a = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    req_b = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    assert request_fingerprint(req_a) == request_fingerprint(req_b)


# -- params_match ------------------------------------------------------------


def test_params_match_true_when_every_field_matches():
    req = _request(max_tokens=256, temperature=0.1, top_p=0.9, stop=["END"])
    stored = {"max_tokens": 256, "temperature": 0.1, "top_p": 0.9, "stop": ["END"], "response_format": None}
    assert params_match(req, stored) is True


def test_params_match_false_when_max_tokens_differs():
    req = _request(max_tokens=256)
    stored = {"max_tokens": 128, "temperature": None, "top_p": None, "stop": None, "response_format": None}
    assert params_match(req, stored) is False


def test_params_match_false_when_temperature_differs():
    req = _request(temperature=0.5)
    stored = {"max_tokens": 1024, "temperature": 0.1, "top_p": None, "stop": None, "response_format": None}
    assert params_match(req, stored) is False
