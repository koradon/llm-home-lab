from collections.abc import Callable
from dataclasses import dataclass, field

from llm_home_lab.backends.base import ChatBackend


@dataclass
class RoutingCandidate:
    backend: ChatBackend
    latency_ms: float
    context_window: int


@dataclass
class RoutingContext:
    task_type: str | None
    token_budget: int


@dataclass
class PolicyRule:
    name: str
    score_fn: Callable[[RoutingCandidate, RoutingContext], float]
    task_type: str | None = None

    def applies_to(self, task_type: str | None) -> bool:
        return self.task_type is None or self.task_type == task_type


@dataclass
class RoutingPolicy:
    rules: list[PolicyRule] = field(default_factory=list)


@dataclass
class RoutingDecision:
    backend_id: str
    scores: dict[str, float]
    matched_rules: dict[str, list[str]]


class NoAvailableBackendError(Exception):
    """No candidate backend is eligible to serve this request."""


__all__ = [
    "NoAvailableBackendError",
    "PolicyRule",
    "RoutingCandidate",
    "RoutingContext",
    "RoutingDecision",
    "RoutingPolicy",
]
