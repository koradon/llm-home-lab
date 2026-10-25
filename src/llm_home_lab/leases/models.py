from dataclasses import dataclass
from datetime import datetime, timedelta

LEASE_HEADER_NAME = "X-LLM-Node-Lease"
# Starlette stores headers lowercase; this is the lookup key.
LEASE_HEADER = "x-llm-node-lease"


@dataclass(frozen=True)
class LeaseSettings:
    """Server-side lease limits.

    ``ttl`` is the short lease a heartbeat renews. ``default_ceiling`` is the total
    hold applied when a reservation does not ask for one. ``max_ceiling`` is the
    longest total hold a reservation is allowed to request.
    """

    ttl: timedelta = timedelta(minutes=3)
    default_ceiling: timedelta = timedelta(hours=3)
    max_ceiling: timedelta = timedelta(hours=3)
    min_active_nodes: int = 1
    expiring_soon: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        if self.default_ceiling <= timedelta(0) or self.max_ceiling < self.default_ceiling:
            raise ValueError(
                "lease max ceiling must be greater than or equal to the default ceiling, "
                "and both must be positive"
            )
        if self.min_active_nodes < 0:
            raise ValueError("min active nodes must be >= 0")
        if self.expiring_soon < timedelta(0):
            raise ValueError("expiring soon window must be >= 0")


@dataclass(frozen=True)
class ReservableHost:
    host_id: str
    status: str
    healthy: bool
    serves_requested_model: bool


@dataclass(frozen=True)
class NodeLease:
    host_id: str
    holder: str
    token: str
    expires_at: datetime
    hard_expires_at: datetime
    created_at: datetime

    def public_dict(self, *, include_token: bool, expiring_soon: bool) -> dict[str, object]:
        body: dict[str, object] = {
            "host_id": self.host_id,
            "holder": self.holder,
            "expires_at": self.expires_at.isoformat(),
            "hard_expires_at": self.hard_expires_at.isoformat(),
            "expiring_soon": expiring_soon,
        }
        if include_token:
            body["token"] = self.token
        return body


class LeaseInvalidError(Exception):
    """The reservation request itself is not acceptable."""


class LeaseRefusedError(Exception):
    """A reservation was refused. The message names the current holders."""


class LeaseNotHeldError(Exception):
    """The presented token does not match a live lease."""


class LeaseEndedError(Exception):
    """The lease expired or reached its hard ceiling."""


class LeaseNotFoundError(Exception):
    """No live lease exists for this operation."""
