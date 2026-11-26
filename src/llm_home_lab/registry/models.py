from dataclasses import dataclass
from datetime import datetime


@dataclass
class ModelLimits:
    max_concurrent_requests: int | None = None
    context_window: int | None = None

    def to_dict(self) -> dict[str, int]:
        result: dict[str, int] = {}
        if self.max_concurrent_requests is not None:
            result["max_concurrent_requests"] = self.max_concurrent_requests
        if self.context_window is not None:
            result["context_window"] = self.context_window
        return result


@dataclass
class HostCapabilities:
    backend_type: str
    context_window: int
    base_url: str
    allowed_models: list[str] | None = None
    memory_budget_gb: float | None = None
    model_sizes_gb: dict[str, float] | None = None
    model_aliases: dict[str, list[str]] | None = None
    # Operator-chosen display order; None sorts after positioned nodes, in registration order.
    position: int | None = None
    model_limits: dict[str, ModelLimits] | None = None

    def __post_init__(self) -> None:
        if self.model_limits is not None:
            self.model_limits = {
                model: limits if isinstance(limits, ModelLimits) else ModelLimits(**limits)
                for model, limits in self.model_limits.items()
            }


@dataclass
class HostCapacity:
    max_concurrent_requests: int


@dataclass
class HostInfo:
    host_id: str
    capabilities: HostCapabilities
    capacity: HostCapacity
    in_flight: int
    last_seen: datetime

    def _resolved_model_limits(self, model: str | None = None) -> ModelLimits | None:
        limits = self.capabilities.model_limits
        if not limits:
            return None
        target = model
        if target is None:
            allowed = self.capabilities.allowed_models
            if allowed is not None and len(allowed) == 1:
                target = allowed[0]
        if target is None:
            return None
        return limits.get(target)

    def effective_max_concurrent_requests(self, model: str | None = None) -> int:
        entry = self._resolved_model_limits(model)
        if entry is not None and entry.max_concurrent_requests is not None:
            return entry.max_concurrent_requests
        return self.capacity.max_concurrent_requests

    def effective_context_window(self, model: str | None = None) -> int:
        entry = self._resolved_model_limits(model)
        if entry is not None and entry.context_window is not None:
            return entry.context_window
        return self.capabilities.context_window


class HostNotRegisteredError(Exception):
    """The referenced host_id has no active registration."""


__all__ = [
    "HostCapabilities",
    "HostCapacity",
    "HostInfo",
    "HostNotRegisteredError",
    "ModelLimits",
]
