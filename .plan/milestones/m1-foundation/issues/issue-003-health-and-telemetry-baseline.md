---
title: Add health endpoints and telemetry baseline
labels:
- observability
- operations
- mvp
state: open
number: 1
state_reason: null
---

## Why

Routing and failover decisions require reliable health and runtime signals.

## Scope

- Add `/health/live` and `/health/ready` endpoints.
- Report backend availability in readiness checks.
- Emit structured logs with request id, backend id, latency, and status.

## Acceptance Criteria

- Liveness and readiness checks are consumable by local monitoring tools.
- Every request has correlated log fields for traceability.
- Failing backends are visible in readiness output.
