---
title: Add monitoring, SLOs, and alerting
labels:
- observability
- sre
- operations
state: open
number: 12
state_reason: null
---

## Why

Reliable operations require visibility into latency, failures, and capacity trends.

## Scope

- Define service-level indicators (availability, p95 latency, failover success).
- Export metrics for dashboards.
- Add actionable alerts for SLO burn and backend saturation.

## Acceptance Criteria

- Dashboards show current and historical orchestrator health.
- Alert rules are tested against simulated incidents.
- On-call runbook links are attached to each critical alert.
