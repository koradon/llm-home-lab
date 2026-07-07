---
title: Implement failover and backend health policy
labels:
- reliability
- operations
- routing
state: open
number: 8
state_reason: null
---

## Why

The orchestrator must continue serving when one backend degrades or goes offline.

## Scope

- Add health score model with probe history.
- Introduce automatic failover with cooldown windows.
- Prevent routing to unhealthy backends until recovery criteria are met.

## Acceptance Criteria

- Simulated backend outage triggers automatic rerouting.
- Unhealthy backend is excluded until health checks pass recovery threshold.
- Failover events are visible in logs and metrics.
