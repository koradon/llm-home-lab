import json
from datetime import UTC, datetime, timedelta

import pytest

from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.models import (
    AlertRule,
    AlertRuleFileError,
    AlertSeverity,
    SliSnapshot,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)

LATENCY_RULE = AlertRule(
    name="p95-latency-threshold",
    kind="threshold",
    metric="p95_latency_ms",
    comparison="gt",
    threshold_value=5000.0,
    window=timedelta(minutes=5),
    severity=AlertSeverity.WARNING,
    runbook_url="docs/runbooks/p95-latency-threshold.md",
)


def _snapshot(p95_latency_ms: float = 0.0) -> SliSnapshot:
    return SliSnapshot(availability=1.0, p95_latency_ms=p95_latency_ms, failover_success_rate=None)


def _snapshot_with_availability(availability: float) -> SliSnapshot:
    return SliSnapshot(availability=availability, p95_latency_ms=0.0, failover_success_rate=None)


def test_a_threshold_rule_does_not_fire_below_its_threshold():
    evaluator = AlertEvaluator([LATENCY_RULE])

    events = evaluator.evaluate(_snapshot(p95_latency_ms=100.0), T0)

    assert events == []


def test_a_threshold_rule_fires_once_when_it_first_breaches():
    evaluator = AlertEvaluator([LATENCY_RULE])

    events = evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)

    assert len(events) == 1
    assert events[0].rule_name == "p95-latency-threshold"
    assert events[0].state == "firing"
    assert events[0].value == 6000.0
    assert events[0].threshold_value == 5000.0
    assert events[0].runbook_url == "docs/runbooks/p95-latency-threshold.md"


def test_a_threshold_rule_does_not_refire_on_consecutive_breaches():
    evaluator = AlertEvaluator([LATENCY_RULE])
    evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)

    events = evaluator.evaluate(_snapshot(p95_latency_ms=7000.0), T0 + timedelta(seconds=30))

    assert events == []


def test_a_threshold_rule_logs_resolved_once_the_metric_recovers():
    evaluator = AlertEvaluator([LATENCY_RULE])
    evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)

    events = evaluator.evaluate(_snapshot(p95_latency_ms=100.0), T0 + timedelta(seconds=30))

    assert len(events) == 1
    assert events[0].state == "resolved"
    assert events[0].value == 100.0


AVAILABILITY_SLO_RULE = AlertRule(
    name="availability-slo-burn",
    kind="slo_burn",
    metric="availability",
    comparison="lt",
    threshold_value=0.99,
    window=timedelta(minutes=5),
    severity=AlertSeverity.CRITICAL,
    runbook_url="docs/runbooks/availability-slo-burn.md",
)


def test_an_slo_burn_rule_fires_when_the_error_rate_exceeds_the_error_budget():
    evaluator = AlertEvaluator([AVAILABILITY_SLO_RULE])

    events = evaluator.evaluate(_snapshot_with_availability(0.9), T0)

    assert len(events) == 1
    assert events[0].rule_name == "availability-slo-burn"
    assert events[0].state == "firing"


def test_an_slo_burn_rule_does_not_fire_within_its_error_budget():
    evaluator = AlertEvaluator([AVAILABILITY_SLO_RULE])

    events = evaluator.evaluate(_snapshot_with_availability(0.999), T0)

    assert events == []


SATURATION_RULE = AlertRule(
    name="host-saturation-threshold",
    kind="threshold",
    metric="host_saturation",
    comparison="gt",
    threshold_value=0.9,
    window=timedelta(minutes=5),
    severity=AlertSeverity.CRITICAL,
    runbook_url="docs/runbooks/host-saturation-threshold.md",
)


def _snapshot_with_saturation(**host_saturation: float) -> SliSnapshot:
    return SliSnapshot(
        availability=1.0,
        p95_latency_ms=0.0,
        failover_success_rate=None,
        host_saturation=host_saturation,
    )


def test_a_per_host_metric_rule_only_fires_for_the_host_that_breaches():
    evaluator = AlertEvaluator([SATURATION_RULE])

    events = evaluator.evaluate(_snapshot_with_saturation(**{"host-a": 0.95, "host-b": 0.5}), T0)

    assert len(events) == 1
    assert events[0].rule_name == "host-saturation-threshold:host-a"
    assert events[0].value == 0.95


def test_a_per_host_metric_rule_tracks_each_hosts_firing_state_independently():
    evaluator = AlertEvaluator([SATURATION_RULE])
    evaluator.evaluate(_snapshot_with_saturation(**{"host-a": 0.95, "host-b": 0.95}), T0)

    events = evaluator.evaluate(
        _snapshot_with_saturation(**{"host-a": 0.1, "host-b": 0.95}), T0 + timedelta(seconds=30)
    )

    assert len(events) == 1
    assert events[0].rule_name == "host-saturation-threshold:host-a"
    assert events[0].state == "resolved"


def test_a_firing_transition_is_logged_via_the_alerts_logger(caplog):
    evaluator = AlertEvaluator([LATENCY_RULE])

    with caplog.at_level("INFO", logger="llm_home_lab.alerts"):
        evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "rule=p95-latency-threshold" in message
    assert "severity=warning" in message
    assert "state=firing" in message
    assert "value=6000.00" in message
    assert "threshold=5000.00" in message
    assert "runbook=docs/runbooks/p95-latency-threshold.md" in message


def test_current_state_is_empty_before_anything_has_fired():
    evaluator = AlertEvaluator([LATENCY_RULE])

    assert evaluator.current_state() == []


def test_current_state_reflects_the_latest_firing_alert():
    evaluator = AlertEvaluator([LATENCY_RULE])
    evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)

    state = evaluator.current_state()

    assert len(state) == 1
    assert state[0].rule_name == "p95-latency-threshold"
    assert state[0].state == "firing"


def test_current_state_drops_a_rule_once_it_resolves():
    evaluator = AlertEvaluator([LATENCY_RULE])
    evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)
    evaluator.evaluate(_snapshot(p95_latency_ms=100.0), T0 + timedelta(seconds=30))

    assert evaluator.current_state() == []


def test_from_file_loads_rules_and_evaluates_them(tmp_path):
    rules_file = tmp_path / "alert_rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "p95-latency-threshold",
                        "kind": "threshold",
                        "metric": "p95_latency_ms",
                        "comparison": "gt",
                        "threshold_value": 5000.0,
                        "window_seconds": 300,
                        "severity": "warning",
                        "runbook_url": "docs/runbooks/p95-latency-threshold.md",
                    }
                ]
            }
        )
    )

    evaluator = AlertEvaluator.from_file(str(rules_file))
    events = evaluator.evaluate(_snapshot(p95_latency_ms=6000.0), T0)

    assert len(events) == 1
    assert events[0].rule_name == "p95-latency-threshold"


def test_from_file_raises_on_an_unknown_rule_kind(tmp_path):
    rules_file = tmp_path / "alert_rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "bogus-rule",
                        "kind": "not_a_real_kind",
                        "metric": "p95_latency_ms",
                        "comparison": "gt",
                        "threshold_value": 5000.0,
                        "window_seconds": 300,
                        "severity": "warning",
                        "runbook_url": "docs/runbooks/bogus.md",
                    }
                ]
            }
        )
    )

    with pytest.raises(AlertRuleFileError):
        AlertEvaluator.from_file(str(rules_file))
