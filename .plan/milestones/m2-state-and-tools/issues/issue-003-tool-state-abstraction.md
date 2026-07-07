---
title: Create model-independent tool state layer
labels:
- tools
- state
- architecture
state: open
number: 7
state_reason: null
---

## Why

Terminal, filesystem, and external tools keep mutable state that must survive model switching.

## Scope

- Define tool session abstractions for filesystem and terminal first.
- Track tool invocation history and relevant outputs.
- Expose replay/continuation hooks for subsequent model calls.

## Acceptance Criteria

- Tool state can be queried and reused across model switches.
- Terminal session continuity (cwd/env/running process metadata) is preserved.
- Integration tests verify continuity across two different model backends.
