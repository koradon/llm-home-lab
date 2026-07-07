---
title: Build workspace state capture pipeline
labels:
- state
- developer-experience
- git
state: open
number: 5
state_reason: null
---

## Why

Model changes should not lose critical coding context.

## Scope

- Capture branch, staged/unstaged diff metadata, open files, and test status.
- Provide normalized snapshot schema for context injection.
- Add pruning strategy for large repositories.

## Acceptance Criteria

- Snapshot can be generated on demand in predictable time.
- Snapshot payload size is bounded and configurable.
- Snapshots are available for prompt assembly and debugging.
