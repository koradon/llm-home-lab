---
title: Implement session manager core
labels:
- core
- state
- memory
state: open
number: 4
state_reason: null
---

## Why

Local models are stateless; orchestration quality depends on centralized memory.

## Scope

- Design session data model (messages, summaries, decisions, constraints).
- Persist session snapshots in local storage.
- Provide APIs for append, read, trim, and summarize.

## Acceptance Criteria

- Session state is restored after orchestrator restart.
- Session APIs are deterministic and documented.
- Automated tests cover persistence and summarization edge cases.
