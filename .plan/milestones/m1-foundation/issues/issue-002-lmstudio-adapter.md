---
title: Build LM Studio backend adapter
labels:
- backend
- integration
- mvp
state: open
number: 2
state_reason: null
---

## Why

The orchestrator needs a stable backend contract to route requests to local model hosts.

## Scope

- Add adapter interface for model backends.
- Implement LM Studio adapter for at least one host.
- Support timeout, retry policy, and normalized error mapping.

## Acceptance Criteria

- Adapter can execute prompt requests against configured LM Studio endpoint.
- Timeout and transport failures are classified and logged.
- Adapter behavior is covered by integration tests with mocked backend responses.
