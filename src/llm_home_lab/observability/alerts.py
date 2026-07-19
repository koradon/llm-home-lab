import json
import logging
from datetime import datetime, timedelta
from typing import Literal

from llm_home_lab.observability.models import (
    AlertEvent,
    AlertRule,
    AlertRuleFileError,
    AlertSeverity,
    SliSnapshot,
)

alerts_logger = logging.getLogger("llm_home_lab.alerts")

_VALID_KINDS = {"threshold", "slo_burn"}


def _rule_from_dict(data: dict) -> AlertRule:
    if data["kind"] not in _VALID_KINDS:
        raise AlertRuleFileError(f"unknown alert rule kind: {data['kind']!r}")
    return AlertRule(
        name=data["name"],
        kind=data["kind"],
        metric=data["metric"],
        comparison=data["comparison"],
        threshold_value=data["threshold_value"],
        window=timedelta(seconds=data["window_seconds"]),
        severity=AlertSeverity(data["severity"]),
        runbook_url=data["runbook_url"],
    )


def _breaches(rule: AlertRule, value: float) -> bool:
    if rule.comparison == "gt":
        return value > rule.threshold_value
    return value < rule.threshold_value


class AlertEvaluator:
    def __init__(self, rules: list[AlertRule]) -> None:
        self._rules = rules
        self._firing: dict[str, bool] = {}
        self._active_events: dict[str, AlertEvent] = {}

    @classmethod
    def from_file(cls, path: str) -> "AlertEvaluator":
        with open(path) as f:
            data = json.load(f)
        return cls([_rule_from_dict(rule) for rule in data["rules"]])

    def evaluate(self, snapshot: SliSnapshot, at: datetime) -> list[AlertEvent]:
        events = []
        for rule in self._rules:
            metric_value = getattr(snapshot, rule.metric)
            if isinstance(metric_value, dict):
                for key, value in metric_value.items():
                    event = self._evaluate_one(rule, f"{rule.name}:{key}", value, at)
                    if event is not None:
                        events.append(event)
            else:
                event = self._evaluate_one(rule, rule.name, metric_value, at)
                if event is not None:
                    events.append(event)

        return events

    def _evaluate_one(
        self, rule: AlertRule, state_key: str, value: float, at: datetime
    ) -> AlertEvent | None:
        breached = _breaches(rule, value)
        was_firing = self._firing.setdefault(state_key, False)

        state: Literal["firing", "resolved"]
        if breached and not was_firing:
            self._firing[state_key] = True
            state = "firing"
        elif not breached and was_firing:
            self._firing[state_key] = False
            state = "resolved"
        else:
            return None

        event = AlertEvent(
            rule_name=state_key,
            severity=rule.severity,
            state=state,
            value=value,
            threshold_value=rule.threshold_value,
            runbook_url=rule.runbook_url,
            at=at,
        )
        alerts_logger.info(
            "rule=%s severity=%s state=%s value=%.2f threshold=%.2f runbook=%s",
            event.rule_name,
            event.severity,
            event.state,
            event.value,
            event.threshold_value,
            event.runbook_url,
        )
        if state == "firing":
            self._active_events[state_key] = event
        else:
            del self._active_events[state_key]
        return event

    def current_state(self) -> list[AlertEvent]:
        return list(self._active_events.values())
