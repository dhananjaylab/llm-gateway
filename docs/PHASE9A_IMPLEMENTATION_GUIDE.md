# LLM Gateway — Phase 9a: Tiers & Pricing Hot-Reload
## Concise Implementation Guide

**Status:** built, tested, passing (**382 passed, 8 skipped** — up from Phase 8b's 346/8; regression-clean, zero
flakiness across 3 consecutive full-suite runs). `ruff check` clean on every one of the 7 files this phase
touched — the repo's full 14 pre-existing findings are unchanged and confirmed to sit entirely outside this
phase's file set (`app/resilience/health.py`, `test_budget_enforcement.py`, `test_ci_workflows.py`,
`test_circuit_breaker.py`, `test_gemini_provider.py`, `test_health_checking.py` — the exact same six files
`PHASE8B_SIGNOFF.md` §4 already named).

**Source:** `docs/PHASE9_KICKOFF_SCOPING.md` Track A, split out as its own phase per that document's §7 decision 1
(hot-reload is small and low-risk; semantic caching — Track B — becomes Phase 9b with its own sign-off gate).

**Baseline verified before any code was written, not assumed:** reconstructed the actual post-Phase-8b repo from
the uploaded export, ran its own test suite cold — **346 passed, 8 skipped**, byte-for-byte matching
`PHASE8B_SIGNOFF.md`'s own claimed number — before touching anything.

---

## Build tasks — what shipped

### 1. `TiersConfigStore` (`app/core/tiers_store.py`, new)

Redis-backed hot-reload for `config/tiers.yaml`. Deliberately **not** a copy of `TeamConfigStore`'s per-key,
short-TTL read-through cache — `FallbackRouter.resolve_chain()` reads the tiers table on the hot path of every
single request, so a Redis round trip there would work against the gateway's own <10ms overhead SLA. Instead: an
**RCU (read-copy-update)** design — the whole table stays resident in the exact `TiersConfig` instance
`FallbackRouter` is constructed with, and a change (an Admin `PATCH`, or a pub/sub event from another instance's
`PATCH`) swaps that instance's `.tiers` dict in one GIL-atomic attribute reassignment, built off to the side first
so no concurrent request ever observes a half-updated table.

**Net effect, confirmed by diff against the untouched Phase 8b export, not assumed:** `app/resilience/fallback.py`
is **byte-identical**. `resolve_chain()`'s body never changed — it's calling the same method on the same object;
only that object's internal dict has been hot-swapped underneath it.

### 2. `PricingStore` (`app/core/pricing_store.py`, new)

Same RCU idea, one sharper constraint: `app.state.pricing` is a bare dict, and `FallbackRouter.__init__` captures
a **reference** to that exact dict object. Reassigning `app.state.pricing` to a new dict on refresh would leave
`FallbackRouter` reading stale prices forever — a real, quiet bug class. `refresh()` therefore does
`table.clear(); table.update(new_entries)` **in place**, with no `await` between the two calls. Confirmed by a
dedicated regression test (`test_refresh_mutates_the_same_dict_object_not_a_new_one`, asserting `id(table)` is
unchanged) and, end-to-end, by diffing `app/core/pricing.py` against the untouched export — also byte-identical.

### 3. Admin API (`app/api/admin.py`)

`GET/PATCH /admin/tiers/{tier_name}` and `GET/PATCH /admin/pricing/{model_key}`, plus `GET /admin/tiers` /
`GET /admin/pricing` list endpoints — same shape as every existing pair (`/admin/limits`, `/admin/budgets`,
`/admin/orgs`): 404 on an unknown key, audit-logged (`patch_tier` / `patch_pricing`, reusing `AuditLog`'s
`team_id` field for a tier name or model key, same precedent `/admin/orgs` already set for an `org_id`), PATCH
only mutates an *existing* row — adding a brand-new tier or model still needs a YAML edit + a fresh Redis, the
same limitation teams/orgs already carry.

### 4. `app/main.py` wiring

Both stores are seeded (idempotent, skips if Redis already has rows) and `refresh()`-ed **before**
`FallbackRouter` is constructed, so it never sees a stale YAML-only snapshot even on a fresh boot. A new,
dedicated pub/sub listener helper, `_listen_for_full_refresh`, is added **alongside** (not modifying) the
existing `_listen_for_store_changes` — the existing helper's `invalidate` callback is called synchronously
without `await`; these two new stores need an `await`-able `refresh()`, and extending the existing helper's
contract would have touched two already-tested, already-locked-down call sites (team, org) for no benefit to
them.

---

## What's explicitly *not* in this phase

Semantic caching (Track B) — `docs/PHASE9_KICKOFF_SCOPING.md` §3, its own phase (9b) with its own sign-off gate,
per that document's §7 decision 1.

---

## New/changed files (this delivery only)

```
app/core/tiers_store.py          # NEW — TiersConfigStore (RCU hot-reload)
app/core/pricing_store.py        # NEW — PricingStore (RCU hot-reload, in-place mutation)
app/api/admin.py                 # +GET/PATCH /admin/tiers/{name}, /admin/pricing/{model_key}, list endpoints
app/main.py                      # wiring: seed+refresh both stores, two new full-refresh listeners

tests/unit/test_tiers_store.py   # NEW — 9 tests, including the FallbackRouter-sees-the-PATCH regression guard
tests/unit/test_pricing_store.py # NEW — 10 tests, including the id(table)-unchanged regression guard
tests/unit/test_admin_api.py     # +17 tests — full HTTP-pipeline coverage of the 4 new route pairs

docs/PHASE9A_IMPLEMENTATION_GUIDE.md   # this file
```

No file outside this list was modified. `app/resilience/fallback.py`, `app/providers/*.py`,
`app/ratelimit/limiter.py`, `app/ratelimit/budget.py`, and `app/core/pricing.py` are confirmed byte-identical to
the Phase 8b baseline by direct diff.

---

## Done criteria — status

- [x] All pre-Phase-9a tests pass unchanged in behavior (346/8 baseline fully intact within the new 382/8 total)
- [x] `app/resilience/fallback.py` — zero lines changed, confirmed by diff, not assumed
- [x] A `PATCH /admin/tiers/{name}` takes effect on the very next `resolve_chain()` call with no restart, proven
      both at the unit level (a `FallbackRouter` built *before* the patch still sees it, zero reconstruction) and
      end-to-end through the real HTTP pipeline
- [x] A `PATCH /admin/pricing/{model_key}` takes effect on the very next `calculate_cost_usd()` call, same
      double coverage, plus the `id(table)`-unchanged regression guard proving in-place mutation specifically
- [x] `ruff check` clean on every file this phase touched
- [x] Zero flakiness across 3 consecutive full-suite runs

## Open items for Phase 9b kickoff

Everything in `docs/PHASE9_KICKOFF_SCOPING.md` §7 decisions 2–7 (isolation strategy, similarity threshold,
double opt-in, temperature gating, streaming/tool-call exclusion scope, `fakeredis` `FILTER`/`SETATTR`
verification) — none of it was needed for this phase and none of it is resolved by it; Track A shipping cleanly
doesn't pre-judge any of Track B's still-open calls.
