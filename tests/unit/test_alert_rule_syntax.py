"""
test_alert_rule_syntax.py

Verifies the Phase 4 test plan: "Prometheus alert rule YAML parses and
evaluates against a synthetic metrics fixture (promtool test rules)".

SCOPE NOTE: `promtool` is a Go binary shipped with Prometheus itself, not
a pip package, and isn't available in this test environment. Rather than
skip this test-plan line entirely, it's implemented as two things
`promtool check rules` would ALSO catch, without needing the real binary:
(1) the YAML is valid and every rule has the fields Prometheus requires
(`alert`, `expr`, `labels`, `annotations` with `summary`/`description`),
and (2) every `gen_ai_*` metric name referenced in an `expr` string is one
this codebase actually declares in app/observability/metrics.py --
catching the single most likely real mistake (a typo'd or renamed metric
that would make a rule silently evaluate to nothing, forever). Full
semantic PromQL validation and evaluation against a synthetic fixture is
deferred to Phase 5's containerized CI, which has an actual Prometheus
toolchain available.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.observability.metrics import build_metrics

_ALERTS_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prometheus" / "alerts.yml"
_ALERTMANAGER_PATH = Path(__file__).resolve().parents[2] / "deploy" / "alertmanager" / "alertmanager.yml"

_METRIC_NAME_RE = re.compile(r"\bgen_ai_[a-z_]+\b")


@pytest.fixture(scope="module")
def alerts_doc() -> dict:
    assert _ALERTS_PATH.exists(), f"expected {_ALERTS_PATH} to exist"
    with open(_ALERTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def declared_metric_names() -> set[str]:
    """The ground truth: every metric name this codebase actually
    registers, read off a real (throwaway) CollectorRegistry rather than
    hand-copied into this test, so a renamed metric in metrics.py fails
    THIS test immediately instead of silently desyncing from alerts.yml."""
    metrics = build_metrics()
    names = set()
    for collector in metrics.registry._collector_to_names:
        names.update(metrics.registry._collector_to_names[collector])
    # prometheus_client suffixes Counters with _total and Histograms with
    # _bucket/_sum/_count internally; strip those so "declared_metric_names"
    # matches the base names alert expressions actually reference.
    base_names = set()
    for name in names:
        base_names.add(re.sub(r"(_bucket|_sum|_count)$", "", name))
    return base_names


def test_alerts_yaml_parses_and_has_the_expected_group_structure(alerts_doc):
    assert "groups" in alerts_doc
    assert len(alerts_doc["groups"]) >= 1
    for group in alerts_doc["groups"]:
        assert "name" in group
        assert "rules" in group
        assert len(group["rules"]) >= 1


def test_every_rule_has_the_fields_prometheus_requires(alerts_doc):
    for group in alerts_doc["groups"]:
        for rule in group["rules"]:
            assert "alert" in rule, f"rule missing 'alert' name: {rule}"
            assert "expr" in rule and rule["expr"].strip(), f"{rule['alert']} has an empty expr"
            assert "for" in rule, f"{rule['alert']} is missing 'for'"
            assert "labels" in rule and "severity" in rule["labels"], f"{rule['alert']} missing severity"
            assert rule["labels"]["severity"] in {"critical", "warning"}, rule["alert"]
            assert "annotations" in rule
            assert "summary" in rule["annotations"], f"{rule['alert']} missing annotations.summary"
            assert "description" in rule["annotations"], f"{rule['alert']} missing annotations.description"


def test_every_referenced_metric_name_is_actually_declared(alerts_doc, declared_metric_names):
    referenced = set()
    for group in alerts_doc["groups"]:
        for rule in group["rules"]:
            referenced.update(_METRIC_NAME_RE.findall(rule["expr"]))

    assert referenced, "expected at least one gen_ai_* metric name across all rules"
    # A PromQL expression querying a histogram references its
    # `_bucket`/`_sum`/`_count` timeseries, not the base metric name --
    # normalize the same way `declared_metric_names` already does so
    # e.g. "gen_ai_server_request_duration_seconds_bucket" matches the
    # base "gen_ai_server_request_duration_seconds" this codebase declares.
    normalized_referenced = {re.sub(r"(_bucket|_sum|_count)$", "", name) for name in referenced}
    unknown = normalized_referenced - declared_metric_names
    assert not unknown, (
        f"alerts.yml references metric name(s) {unknown} that "
        f"app/observability/metrics.py does not declare — a typo, or metrics.py "
        f"was renamed without updating the alert rule"
    )


def test_the_four_trd_named_conditions_are_all_present(alerts_doc):
    """TRD Phase 4 build task, verbatim: "provider error rate above
    threshold, team approaching budget cap, latency P99 above SLA, and
    circuit breaker opening" -- one rule per condition, at minimum."""
    alert_names = {rule["alert"] for group in alerts_doc["groups"] for rule in group["rules"]}
    assert "CircuitBreakerOpen" in alert_names
    assert "GatewayLatencyP99AboveSLA" in alert_names
    assert "TeamApproachingBudgetCap" in alert_names
    assert any("Failover" in name or "Error" in name for name in alert_names), (
        "expected a provider-error-rate-shaped alert (see alerts.yml header "
        "for why this is approximated via fallback-event rate, not a direct "
        "per-provider error counter)"
    )


def test_alertmanager_yaml_parses_and_every_route_receiver_exists():
    assert _ALERTMANAGER_PATH.exists(), f"expected {_ALERTMANAGER_PATH} to exist"
    with open(_ALERTMANAGER_PATH, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    receiver_names = {r["name"] for r in doc["receivers"]}
    assert doc["route"]["receiver"] in receiver_names
    for route in doc["route"].get("routes", []):
        assert route["receiver"] in receiver_names, f"undefined receiver: {route['receiver']}"

    # Slack webhook is read from a mounted file, never embedded in the
    # checked-in config -- see alertmanager.yml's own header.
    assert "slack_api_url_file" in doc["global"]
    assert "slack_api_url" not in doc["global"]


# -- Grafana dashboards -------------------------------------------------
# Not in the TRD's own named test-file list (Document 06 Phase 4 marks
# "Manual dashboard QA" as an explicit checklist item, not an automated
# test -- it needs a real Grafana to confirm every panel resolves with no
# "no data"). What CAN be automated without one, and is folded in here
# per this project's established "quality improvements found during
# sign-off are folded into the baseline" pattern: the JSON is valid, and
# -- the same class of bug the alerts.yml checks above catch -- every
# metric name a panel's PromQL queries actually exists.

_DASHBOARDS_DIR = Path(__file__).resolve().parents[2] / "deploy" / "grafana" / "dashboards"


@pytest.mark.parametrize("filename", ["operations.json", "business.json", "performance.json"])
def test_dashboard_json_is_valid_and_every_panel_metric_is_declared(filename, declared_metric_names):
    import json

    path = _DASHBOARDS_DIR / filename
    assert path.exists(), f"expected {path} to exist"
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    assert doc["panels"], f"{filename} has no panels"
    referenced = set()
    for panel in doc["panels"]:
        for target in panel.get("targets", []):
            referenced.update(_METRIC_NAME_RE.findall(target.get("expr", "")))

    assert referenced, f"{filename}: expected at least one gen_ai_* metric reference"
    normalized = {re.sub(r"(_bucket|_sum|_count)$", "", name) for name in referenced}
    unknown = normalized - declared_metric_names
    assert not unknown, f"{filename} references undeclared metric(s): {unknown}"
