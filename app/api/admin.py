"""
Admin API (TRD Phase 2 build task: "/admin/limits/{team} and
/admin/budgets/{team} (GET current state, PATCH new caps) and
/admin/audit... Every write hot-reloads the in-memory routing table
without a restart").

Auth: every /admin/* route requires `X-Gateway-Admin-Key`, a single
elevated secret distinct from any team key (Document 05, "Auth model").
Document 03's App Flow doc describes admin auth as "required in addition
to (not instead of) normal request validation for read routes that also
accept a team key" — Phase 2 takes the simpler, unambiguous reading of
that: every /admin/* route (read and write) requires ONLY the admin key,
never a team key. No admin route in this phase accepts a team key at all,
so there is nothing to layer the admin check "in addition to" — this is
flagged explicitly as an interpretation, not left silently ambiguous.

Team provisioning (creating a brand-new team) is deliberately NOT part of
this API — PATCH only ever updates an *existing* team's limits/budget and
404s otherwise. Provisioning is a distinct, out-of-scope-for-Phase-2
operation (see scripts/hash_api_key.py + scripts/seed_teams.py for the
current manual path); wiring a POST /admin/teams is a natural, small
Phase 5/6 addition once the demo-team-seeding story in Phase 5 needs it.

Phase 3 addition: GET /admin/health and GET /admin/circuits, read-only,
same admin-key gate as everything else here. Not spelled out in Document
03's Phase 3 surface inventory (that document only lists /admin/limits,
/admin/budgets, /admin/audit) — added on top per explicit developer
sign-off ahead of Phase 4's Grafana Operations dashboard, so Phase 3's
own done criteria ("a simulated provider outage results in the client
receiving 200 from the fallback, end-to-end") can be verified by hand
without waiting for Phase 4. Both endpoints report on every provider-model
pair reachable from config/tiers.yaml's chains (deduplicated,
unconfigured providers silently skipped — see
app/providers/registry.py::all_configured_provider_models), not on
whatever a client happened to request recently, so an admin can see a
provider's state even if nothing has called it yet this process lifetime.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import get_gateway_settings
from app.providers.registry import all_configured_provider_models

logger = logging.getLogger("gateway.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


async def require_admin(
    x_gateway_admin_key: str = Header(..., alias="X-Gateway-Admin-Key"),
) -> str:
    """
    Dependency for every /admin/* route. Returns the resolved actor
    identity for the audit log. Phase 2 has exactly one shared admin
    secret, so the actor is the literal string "admin"; per-admin
    keys/SSO in a later phase would resolve a real principal here instead
    without changing any route's signature.
    """
    settings = get_gateway_settings()
    if not settings.gateway_admin_key or x_gateway_admin_key != settings.gateway_admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "type": "invalid_admin_key",
                    "message": "Unknown or invalid X-Gateway-Admin-Key.",
                }
            },
        )
    return "admin"


# -- limits ----------------------------------------------------------------


class TeamLimitsView(BaseModel):
    team_id: str
    rpm_cap: int
    tpm_cap: int
    remaining_rpm: int
    remaining_tpm: int


class TeamLimitsPatch(BaseModel):
    rpm_cap: int | None = Field(default=None, gt=0)
    tpm_cap: int | None = Field(default=None, gt=0)


async def _limits_view(team_id: str, team, request: Request) -> TeamLimitsView:
    peek = await request.app.state.rate_limiter.peek(team)
    return TeamLimitsView(
        team_id=team_id,
        rpm_cap=team.rpm_cap,
        tpm_cap=team.tpm_cap,
        remaining_rpm=peek.remaining_rpm or 0,
        remaining_tpm=peek.remaining_tpm or 0,
    )


@router.get("/limits/{team_id}", response_model=TeamLimitsView)
async def get_limits(team_id: str, request: Request, actor: str = Depends(require_admin)) -> TeamLimitsView:
    team = await request.app.state.team_store.get_team(team_id)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"error": {"type": "team_not_found"}})
    return await _limits_view(team_id, team, request)


@router.patch("/limits/{team_id}", response_model=TeamLimitsView)
async def patch_limits(
    team_id: str, patch: TeamLimitsPatch, request: Request, actor: str = Depends(require_admin)
) -> TeamLimitsView:
    store = request.app.state.team_store
    before = await store.get_team(team_id)
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"error": {"type": "team_not_found"}})

    changes = patch.model_dump(exclude_none=True)
    if not changes:
        return await _limits_view(team_id, before, request)

    try:
        after = await store.update_team(team_id, changes)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": {"type": "team_not_found"}}
        ) from exc

    await request.app.state.audit_log.record(
        actor=actor,
        action="patch_limits",
        team_id=team_id,
        before={"rpm_cap": before.rpm_cap, "tpm_cap": before.tpm_cap},
        after={"rpm_cap": after.rpm_cap, "tpm_cap": after.tpm_cap},
    )

    return await _limits_view(team_id, after, request)


# -- budgets -----------------------------------------------------------------


class TeamBudgetView(BaseModel):
    team_id: str
    budget_cap_usd: float
    budget_period: str
    spend_usd: float
    period_label: str


class TeamBudgetPatch(BaseModel):
    budget_cap_usd: float | None = Field(default=None, gt=0)


async def _budget_view(team_id: str, team, request: Request) -> TeamBudgetView:
    decision = await request.app.state.budget_enforcer.precheck(team)
    return TeamBudgetView(
        team_id=team_id,
        budget_cap_usd=decision.cap_usd,
        budget_period=team.budget_period,
        spend_usd=decision.spend_usd,
        period_label=decision.period,
    )


@router.get("/budgets/{team_id}", response_model=TeamBudgetView)
async def get_budget(team_id: str, request: Request, actor: str = Depends(require_admin)) -> TeamBudgetView:
    team = await request.app.state.team_store.get_team(team_id)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"error": {"type": "team_not_found"}})
    return await _budget_view(team_id, team, request)


@router.patch("/budgets/{team_id}", response_model=TeamBudgetView)
async def patch_budget(
    team_id: str, patch: TeamBudgetPatch, request: Request, actor: str = Depends(require_admin)
) -> TeamBudgetView:
    store = request.app.state.team_store
    before = await store.get_team(team_id)
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"error": {"type": "team_not_found"}})

    changes = patch.model_dump(exclude_none=True)
    if not changes:
        return await _budget_view(team_id, before, request)

    try:
        after = await store.update_team(team_id, changes)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": {"type": "team_not_found"}}
        ) from exc

    await request.app.state.audit_log.record(
        actor=actor,
        action="patch_budget",
        team_id=team_id,
        before={"budget_cap_usd": before.budget_cap_usd},
        after={"budget_cap_usd": after.budget_cap_usd},
    )

    return await _budget_view(team_id, after, request)


# -- audit ---------------------------------------------------------------


class AuditEntryView(BaseModel):
    id: str
    actor: str | None
    action: str | None
    team_id: str | None
    before: dict | None
    after: dict | None
    ts: float


@router.get("/audit", response_model=list[AuditEntryView])
async def get_audit(
    request: Request, limit: int = 50, actor: str = Depends(require_admin)
) -> list[AuditEntryView]:
    entries = await request.app.state.audit_log.recent(limit=limit)
    return [AuditEntryView(**e) for e in entries]


# -- Phase 3: health + circuits (read-only, ahead of Phase 4 Grafana) --------


class HealthStatusView(BaseModel):
    provider: str
    model: str
    state: str  # "healthy" | "degraded" | "down" | "unknown"
    sample_count: int
    error_rate: float | None
    p99_latency_ms: float | None
    last_checked_at: float | None


class CircuitStatusView(BaseModel):
    provider: str
    model: str
    state: str  # "closed" | "open" | "half_open"
    opened_at: float | None
    failures_in_window: int
    window_size: int


def _known_provider_models(request: Request) -> list[tuple[str, str]]:
    """Every (provider, model) pair reachable from config/tiers.yaml's
    chains in this environment, deduplicated, skipping any provider whose
    API key isn't configured."""
    tiers_config = request.app.state.tiers_config
    resolved = all_configured_provider_models(tiers_config.all_links())
    return [(provider, model) for _model_id, provider, model in resolved]


@router.get("/health", response_model=list[HealthStatusView])
async def get_health(request: Request, actor: str = Depends(require_admin)) -> list[HealthStatusView]:
    tracker = request.app.state.health_tracker
    statuses = await tracker.list_status(_known_provider_models(request))
    return [
        HealthStatusView(
            provider=s.provider,
            model=s.model,
            state=s.state,
            sample_count=s.sample_count,
            error_rate=s.error_rate,
            p99_latency_ms=s.p99_latency_ms,
            last_checked_at=s.last_checked_at,
        )
        for s in statuses
    ]


@router.get("/circuits", response_model=list[CircuitStatusView])
async def get_circuits(request: Request, actor: str = Depends(require_admin)) -> list[CircuitStatusView]:
    circuit_breaker = request.app.state.circuit_breaker
    statuses = await circuit_breaker.list_status(_known_provider_models(request))
    return [
        CircuitStatusView(
            provider=s.provider,
            model=s.model,
            state=s.state,
            opened_at=s.opened_at,
            failures_in_window=s.failures_in_window,
            window_size=s.window_size,
        )
        for s in statuses
    ]
