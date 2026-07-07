---
title: Implement OpenAI-compatible API gateway
labels:
- backend
- api
- mvp
state: open
number: 3
state_reason: null
---

## Why

Agents should integrate once and never care where models run.

## Scope

- Implement `/v1/chat/completions` compatibility layer.
- Validate request schema and map it to internal orchestration format.
- Return OpenAI-style response payloads and error structure.

## Acceptance Criteria

- Existing OpenAI-compatible clients can call the endpoint without code changes.
- Validation errors use consistent machine-readable error format.
- Unit tests cover happy path and invalid payloads.
