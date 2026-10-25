import os
from collections.abc import Mapping
from datetime import timedelta

from llm_home_lab.leases.models import LeaseSettings


def lease_settings_from_env(env: Mapping[str, str] | None = None) -> LeaseSettings:
    values = os.environ if env is None else env
    return LeaseSettings(
        ttl=_seconds(values, "ORCHESTRATOR_LEASE_TTL_S", "180"),
        default_ceiling=_seconds(values, "ORCHESTRATOR_LEASE_CEILING_S", "10800"),
        max_ceiling=_seconds(values, "ORCHESTRATOR_LEASE_MAX_CEILING_S", "10800"),
        min_active_nodes=int(values.get("ORCHESTRATOR_LEASE_MIN_ACTIVE_NODES", "1")),
    )


def _seconds(env: Mapping[str, str], name: str, default: str) -> timedelta:
    return timedelta(seconds=float(env.get(name, default)))
