"""
test_ci_workflows.py

Phase 6 addition. Same rationale as test_alert_rule_syntax.py (Phase 4):
there's no `act` or a real GitHub Actions runner in this test environment
to actually execute .github/workflows/*.yml, but the class of mistake
that matters most for a checked-in CI config — invalid YAML, a job with
no steps, a matrix entry missing a required field, a workflow that
silently references a file this repo doesn't have — is catchable without
one. Full semantic validation (does the workflow actually pass on GitHub's
runners) is inherently something only GitHub Actions itself can confirm;
this test narrows what's left to check by hand after a change to these
files to "did the YAML actually change in the way I meant."
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_DEPENDABOT_PATH = _REPO_ROOT / ".github" / "dependabot.yml"


_YAML_1_1_BOOLS = {
    "yes": True, "Yes": True, "YES": True,
    "true": True, "True": True, "TRUE": True,
    "no": False, "No": False, "NO": False,
    "false": False, "False": False, "FALSE": False,
}
# "on"/"off" (in any casing) are the one corner of YAML 1.1's bool literal
# set that GitHub Actions workflows rely on staying a plain STRING -- `on:`
# is the trigger-list key, and `on: true` never appears anywhere in a real
# workflow. Excluding just those two from the constructor below (while
# still resolving true/false/yes/no to real Python bools, e.g. `push: false`
# in ci.yml's docker-build step) is what keeps `doc["on"]` a normal dict
# lookup instead of silently needing to become `doc[True]`.
_KEEP_AS_STRING = {"on", "On", "ON", "off", "Off", "OFF"}


def _bool_constructor(loader: yaml.SafeLoader, node: yaml.ScalarNode):
    value = loader.construct_scalar(node)
    if value in _KEEP_AS_STRING:
        return value
    return _YAML_1_1_BOOLS.get(value, value)


class _WorkflowSafeLoader(yaml.SafeLoader):
    pass


_WorkflowSafeLoader.add_constructor("tag:yaml.org,2002:bool", _bool_constructor)


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_WorkflowSafeLoader)


@pytest.fixture(scope="module")
def ci_doc() -> dict:
    assert (_WORKFLOWS_DIR / "ci.yml").exists()
    return _load(_WORKFLOWS_DIR / "ci.yml")


@pytest.fixture(scope="module")
def integration_doc() -> dict:
    assert (_WORKFLOWS_DIR / "integration.yml").exists()
    return _load(_WORKFLOWS_DIR / "integration.yml")


# -- ci.yml (the fast, every-push/PR workflow) --------------------------


def test_ci_workflow_triggers_on_push_and_pull_request(ci_doc):
    triggers = ci_doc["on"]
    assert "push" in triggers
    assert "pull_request" in triggers
    assert "main" in triggers["push"]["branches"]
    assert "main" in triggers["pull_request"]["branches"]


def test_ci_workflow_has_lint_test_and_docker_build_jobs(ci_doc):
    jobs = ci_doc["jobs"]
    assert {"lint", "test", "docker-build"} <= jobs.keys()
    for job in jobs.values():
        assert job.get("steps"), "every job must have at least one step"


def test_ci_workflow_pytest_step_runs_the_full_suite_not_a_subset(ci_doc):
    steps = ci_doc["jobs"]["test"]["steps"]
    run_commands = [s.get("run", "") for s in steps]
    assert any(cmd.strip() == "pytest -v" for cmd in run_commands), (
        "the test job must run the whole suite (`pytest -v`), matching the README's "
        "documented `211 passed, 7 skipped` baseline -- not a narrowed-down subset "
        "that would silently stop covering something"
    )


def test_ci_workflow_docker_build_matrix_covers_both_images(ci_doc):
    matrix_images = ci_doc["jobs"]["docker-build"]["strategy"]["matrix"]["image"]
    names = {entry["name"] for entry in matrix_images}
    assert names == {"gateway", "mock-providers"}
    for entry in matrix_images:
        # Both Dockerfiles this job builds must actually exist in the repo
        # -- a typo'd path here would otherwise only surface once this
        # workflow actually runs on GitHub, not from reading the YAML.
        assert (_REPO_ROOT / entry["dockerfile"]).exists(), entry["dockerfile"]

    build_step = ci_doc["jobs"]["docker-build"]["steps"][-1]
    assert build_step["with"]["push"] is False, (
        "this job verifies the Dockerfiles still build -- it must never push an "
        "image anywhere; a registry-publish job is a deliberate non-goal, see "
        "ci.yml's own header comment"
    )


def test_ci_workflow_grants_no_write_permissions(ci_doc):
    """Principle of least privilege: this workflow only reads the repo,
    lints, tests, and builds (never pushes) -- it should never need
    anything beyond `contents: read`."""
    assert ci_doc.get("permissions") == {"contents": "read"}


# -- integration.yml (the heavy, docker-compose workflow) --------------


def test_integration_workflow_does_not_run_on_every_pull_request(integration_doc):
    """The whole point of splitting this into a second workflow (see its
    own header) is that a routine PR must not be blocked on bringing up
    10 containers -- confirm `pull_request` was never wired in here."""
    assert "pull_request" not in integration_doc["on"]
    assert "push" in integration_doc["on"]
    assert "workflow_dispatch" in integration_doc["on"]


def test_integration_workflow_always_tears_down_the_stack(integration_doc):
    for job in integration_doc["jobs"].values():
        down_steps = [
            s
            for s in job["steps"]
            if "docker compose down" in s.get("run", "")
        ]
        assert down_steps, f"job {job.get('name')} never tears the stack down"
        assert down_steps[0].get("if") == "always()", (
            "teardown must run even if an earlier step failed, or a failed CI run "
            "leaks running containers on the runner"
        )


def test_integration_workflow_health_check_uses_the_same_script_the_readme_documents(
    integration_doc,
):
    for job in integration_doc["jobs"].values():
        wait_steps = [s for s in job["steps"] if "verify_stack_healthy.sh" in s.get("run", "")]
        assert wait_steps, f"job {job.get('name')} never waits on scripts/verify_stack_healthy.sh"
    assert (_REPO_ROOT / "scripts" / "verify_stack_healthy.sh").exists()


def test_integration_workflow_load_test_job_is_opt_in_only(integration_doc):
    load_test_job = integration_doc["jobs"]["load-test"]
    assert load_test_job["if"] == "github.event_name == 'workflow_dispatch' && inputs.run_load_test"


# -- dependabot.yml --------------------------------------------------------


def test_dependabot_config_parses_and_covers_every_dependency_surface():
    assert _DEPENDABOT_PATH.exists()
    with open(_DEPENDABOT_PATH, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    ecosystems_and_dirs = {(u["package-ecosystem"], u["directory"]) for u in doc["updates"]}
    assert ("pip", "/") in ecosystems_and_dirs
    assert ("pip", "/deploy/mock-providers") in ecosystems_and_dirs
    assert ("docker", "/") in ecosystems_and_dirs
    assert ("docker", "/deploy/mock-providers") in ecosystems_and_dirs
    assert ("github-actions", "/") in ecosystems_and_dirs

    for update in doc["updates"]:
        assert update["schedule"]["interval"] in {"daily", "weekly", "monthly"}
