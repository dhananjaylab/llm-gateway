# Phase 6 — Concise Implementation Guide

Scope of this phase, per Document 06 plus the Phase 5 sign-off note that
deferred CI here: **CI wiring, the demo recording, and the portfolio
narrative.** No application code changed — every file below is new.

## What shipped

| File | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Fast gate: lint (ruff) + full `pytest` suite + a Dockerfile-build-only check for both images. Runs on every push and PR against `main`. |
| `.github/workflows/integration.yml` | Heavy gate: brings up the full `docker-compose` stack (all 10 services) and runs the real live-stack integration suite (`tests/integration/`). Runs on push to `main`, weekly on a schedule, and on manual dispatch (with an opt-in k6 load-test job). |
| `.github/dependabot.yml` | Weekly update PRs for both `requirements.txt` files, both Dockerfiles' base images, and the two workflow files above. |
| `tests/unit/test_ci_workflows.py` | Structural validation of the three files above — same "config that's shipped as code deserves a test" convention `test_alert_rule_syntax.py` already established in Phase 4. |
| `docs/PHASE6_DEMO_RECORDING_GUIDE.md` | Step-by-step script for recording the <4-minute demo Document 06 asks for. |
| `docs/PHASE6_NARRATIVE.md` | README case-study section, resume bullets, and a LinkedIn/recruiter writeup — with three load-test numbers left as explicit placeholders (see below). |
| `docs/PHASE6_CI_IMPLEMENTATION_GUIDE.md` | This file. |

## Why two workflows, not one

A single workflow that both lints/tests *and* spins up 10 containers would
make every routine PR wait multiple minutes on infrastructure that has
nothing to do with most code changes. Splitting them means:
- `ci.yml` answers "is this safe to merge" in under two minutes, using
  nothing beyond what `pytest -v` already needs (no Redis, no Docker
  Compose — the unit suite is fakeredis-backed by design, see
  `tests/unit/conftest.py`).
- `integration.yml` answers "does the containerized system still actually
  work end-to-end" less often (on merge to `main`, weekly, or on demand),
  which is the right cadence for something that costs several minutes of
  runner time and exercises real chaos injection.

## Decisions made without a separate sign-off round (state them, don't hide them)

- **No registry push.** `ci.yml`'s `docker-build` job builds both images
  with `push: false` — it exists to catch "the Dockerfile doesn't build
  anymore," not to publish anywhere. There's no deployment target for a
  portfolio project to push *to*; wiring GHCR is a two-line addition
  (`docker/login-action@v4` + `push: true`) if that ever changes.
- **`GATEWAY_ADMIN_KEY` in `integration.yml` is a plaintext, publicly-
  visible placeholder** (`ci-admin-key-not-a-secret`), not a GitHub
  Actions secret. This matches `docker-compose.yml`'s own documented
  default (`${GATEWAY_ADMIN_KEY:-dev-admin-key-change-me}`) and every
  provider credential the stack uses in this mode (`mock-key`, pointed at
  `mock-providers`) — nothing this workflow touches is a real credential,
  so there's nothing to protect with a secret.
- **The k6 load-test job is opt-in, not automatic**, even on the weekly
  schedule. It's the heaviest single job in either workflow (several
  minutes, deliberately high concurrency) and its output is meant to be
  *read* (pasted into `docs/PHASE6_NARRATIVE.md`), not just pass/fail —
  automatic execution would bury that signal in routine CI noise instead
  of surfacing it as a deliberate, reviewable artifact upload.
- **Ruff is wired in but `continue-on-error: true`.** `ruff check .` on
  `main` as of this pass reports 11 pre-existing findings (7 auto-fixable
  with `ruff check --fix`): three nested-`with`-statement suggestions in
  the streaming code of `openai_adapter.py`/`anthropic_adapter.py`/
  `ollama_adapter.py`, a redundant `int()` cast in `health.py`, one
  111-character line in a test file, three stale `# noqa: SLF001`
  comments left over from an earlier Phase 3 pass whose rule is no longer
  enabled, and one unsorted import block in `test_gemini_provider.py`.
  None affect runtime behavior. This wasn't fixed as part of Phase 6
  because touching `app/providers/*.py` and four test files wasn't in
  scope for "wire up CI" — **flagging it here as an open item for
  developer sign-off**: run `ruff check --fix .` (7 of 11 resolve
  automatically), hand-fix the remaining 4, and flip `continue-on-error`
  to `false` once clean, so lint actually gates merges going forward
  instead of just reporting.

## Search-first version pins (confirmed this pass, Aug 2026)

| Action | Version | Note |
| --- | --- | --- |
| `actions/checkout` | v7 | v7 became GA in July 2026 with safer `pull_request_target` defaults by default; nothing in either workflow here uses that trigger, so this is a straightforward current-version pin, not a behavior change to account for. |
| `actions/setup-python` | v7 | Matches `actions/checkout@v7`'s minimum runner version (2.327.1+) — GitHub-hosted `ubuntu-latest` runners already exceed this. |
| `actions/upload-artifact` | v7 | Used once, for the opt-in k6 job's output. |
| `docker/setup-buildx-action` | v4 | |
| `docker/build-push-action` | v7 | |

Re-verify all five before this workflow goes stale — `.github/dependabot.yml` (this same delivery) is what turns that from a manual reminder into an automatic PR.

## Test plan for this phase

| Test | Verifies |
| --- | --- |
| `test_ci_workflow_triggers_on_push_and_pull_request` | `ci.yml` fires on the events a fast gate needs to |
| `test_ci_workflow_has_lint_test_and_docker_build_jobs` | All three jobs exist and every job has at least one step |
| `test_ci_workflow_pytest_step_runs_the_full_suite_not_a_subset` | The test job runs `pytest -v` verbatim, not a narrowed `-k` filter that would silently stop covering something |
| `test_ci_workflow_docker_build_matrix_covers_both_images` | Both Dockerfiles are covered, both paths actually exist in the repo, and the build step never pushes |
| `test_ci_workflow_grants_no_write_permissions` | Least-privilege `permissions: contents: read` |
| `test_integration_workflow_does_not_run_on_every_pull_request` | The heavy workflow really is opt-out-of-PRs, matching its own stated rationale |
| `test_integration_workflow_always_tears_down_the_stack` | Every job's `docker compose down` step runs unconditionally (`if: always()`), so a failed run doesn't leak containers on the runner |
| `test_integration_workflow_health_check_uses_the_same_script_the_readme_documents` | No drift between what CI waits on and what a human running the README's own instructions waits on |
| `test_integration_workflow_load_test_job_is_opt_in_only` | The k6 job's `if:` condition matches the documented opt-in design |
| `test_dependabot_config_parses_and_covers_every_dependency_surface` | All five ecosystem/directory pairs are present |

Regression: full suite re-run after this addition — **221 passed, 7
skipped** (211 carried forward from Phase 5, unchanged, + 10 new).

## Done criteria — status

- [x] CI wired: fast lint+test gate on every push/PR; full containerized
      integration suite on push to `main`, weekly, and on demand
- [x] Every new file has a test verifying its structure
- [x] Full regression suite green (221/221, 7 correctly skipped without a
      live stack)
- [ ] **Requires a human with a real GitHub remote**: pushing these files
      and confirming both workflows actually go green on GitHub's own
      runners — this delivery validated the YAML structurally and the
      commands locally (the same `pytest -v`, `ruff check .`, and
      `docker compose` commands the workflows invoke), but, per this
      repo's own established "known limitation" pattern from Phase 5, a
      real run on GitHub Actions itself is the final confirmation this
      environment can't produce standalone.
- [ ] Demo recorded (`docs/PHASE6_DEMO_RECORDING_GUIDE.md` ships the
      script; recording it is a human, on-camera step)
- [ ] Narrative published with real numbers (`docs/PHASE6_NARRATIVE.md`
      ships with three `[PLACEHOLDER]`s that need one real
      `docker compose --profile load-test run --rm k6` output to fill in)
