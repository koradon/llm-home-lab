---
title: Build multi-node registry and scheduler
labels:
- distributed
- scheduler
- infrastructure
state: open
number: 10
state_reason: null
---

## Why

A single control plane needs visibility and control over multiple local model hosts.

## Scope

- Register model hosts with capabilities and capacity metadata.
- Implement scheduling queue with fairness and priority support.
- Add host heartbeat and automatic de-registration on timeout.

## Acceptance Criteria

- New hosts can join and leave without orchestrator restart.
- Scheduler distributes requests according to policy and node capacity.
- Node metadata is queryable for diagnostics.
