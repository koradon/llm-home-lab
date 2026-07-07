---
title: M2 - Stateful Session and Tool Context
description: Introduce orchestrator-owned session memory, workspace state, and model-independent
  tool state.
state: open
number: 2
---

# M2 - Stateful Session and Tool Context

Goal: make model switching safe by keeping all operational context in the orchestrator.

## Exit Criteria

- Session manager persists conversation and decision context.
- Workspace state captures git, files, and execution status snapshot.
- Tool state persists independently from selected model.
- Context compaction/summarization keeps token usage bounded.
