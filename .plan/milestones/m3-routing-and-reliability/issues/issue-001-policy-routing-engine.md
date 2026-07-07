---
title: Implement policy-based routing engine
labels:
- routing
- core
- performance
state: open
number: 6
state_reason: null
---

## Why

Different models serve different tasks; routing should optimize quality and speed.

## Scope

- Implement pluggable routing policy inputs (task type, token budget, latency).
- Add backend scoring and selection algorithm.
- Support sticky model preference for active sessions.

## Acceptance Criteria

- Requests are routed according to declared policy rules.
- Routing decisions are reproducible in test scenarios.
- Sticky session behavior can be enabled/disabled by configuration.
