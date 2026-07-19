from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal


@dataclass
class SliSnapshot:
    availability: float
    p95_latency_ms: float
    failover_success_rate: float | None
    host_saturation: dict[str, float] = field(default_factory=dict)
    queue_depth: int = 0
    token_usage_total: dict[str, int] = field(default_factory=dict)


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"


@dataclass
class AlertRule:
    name: str
    kind: Literal["threshold", "slo_burn"]
    metric: str
    comparison: Literal["gt", "lt"]
    threshold_value: float
    window: timedelta
    severity: AlertSeverity
    runbook_url: str


@dataclass
class AlertEvent:
    rule_name: str
    severity: AlertSeverity
    state: Literal["firing", "resolved"]
    value: float
    threshold_value: float
    runbook_url: str
    at: datetime


class AlertRuleFileError(Exception):
    """The alert rules file is malformed (invalid JSON or an unknown rule kind)."""
