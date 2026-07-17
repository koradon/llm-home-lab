from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProbeResult:
    healthy: bool
    at: datetime


@dataclass
class FailoverEvent:
    backend_id: str
    from_healthy: bool
    to_healthy: bool
    at: datetime


@dataclass
class BackendHealthState:
    is_healthy: bool = True
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    unhealthy_since: datetime | None = None
    history: deque[bool] = field(default_factory=lambda: deque(maxlen=20))


__all__ = ["BackendHealthState", "FailoverEvent", "ProbeResult"]
