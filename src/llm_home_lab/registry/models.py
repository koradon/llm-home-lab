from dataclasses import dataclass
from datetime import datetime


@dataclass
class HostCapabilities:
    backend_type: str
    context_window: int
    base_url: str


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


class HostNotRegisteredError(Exception):
    """The referenced host_id has no active registration."""


__all__ = ["HostCapabilities", "HostCapacity", "HostInfo", "HostNotRegisteredError"]
