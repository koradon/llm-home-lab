# Tool State Abstraction

## Status

draft

## Summary

A passive, per-session invocation history for terminal and filesystem tool actions, so that a
different model picking up a session can see what commands were run, what files were touched,
and (for terminal specifically) the current working directory and environment — without the
orchestrator ever running a shell or touching a filesystem itself. Per the
[orchestrator concept](../../Local_LLM_Orchestrator_Concept.md), real tool execution lives on the
agent's side; this module only normalizes, orders, and persists what the caller reports.

## User stories

- As the agent, I want to report a terminal command's cwd, exit code, and output after running
  it, so that a session's terminal continuity survives a model switch.
- As the agent, I want to report a filesystem operation (read/write/delete) and its result, so
  that a later model can see what files were already touched in this session.
- As the orchestrator, I want a session's current terminal state (cwd, env, running processes)
  derivable from its invocation history, so that I don't need a second, separately-maintained
  "current state" table that could drift from the history.

## Requirements

- Provide a `ToolStateManager` with an async API: `record_terminal_invocation`,
  `record_filesystem_invocation`, `read_terminal_history`, `read_filesystem_history`,
  `read_terminal_state`.
- Persist invocations in the same SQLite file as session/workspace state (see
  [ADR-0002](../adr/0002-sqlite-for-session-storage.md)), in one `tool_invocations` table shared
  by both tool types, discriminated by a `tool_id` column (`"terminal"` or `"filesystem"`).
- Invocations are ordered by a single per-session monotonic `seq`, shared across both tool
  types (not a separate counter per tool) — this preserves true chronological order when
  terminal and filesystem actions interleave, which matters for faithful replay.
- `record_terminal_invocation(session_id, command, cwd, exit_code, output, env=None,
  running_processes=None)` appends a `TerminalInvocation` record. `env` defaults to `{}`,
  `running_processes` defaults to `[]`.
- `record_filesystem_invocation(session_id, operation, path, result)` appends a
  `FilesystemInvocation` record.
- `read_terminal_history(session_id)` / `read_filesystem_history(session_id)` return that
  session's invocations of the matching tool type, in `seq` order.
- `read_terminal_state(session_id)` returns a `TerminalState` (`cwd`, `env`,
  `running_processes`, `as_of_seq`) derived from the most recent terminal invocation, or `None`
  if no terminal invocation has been recorded yet for that session.
- All five methods raise `SessionNotFoundError` if `session_id` does not exist in the `sessions`
  table.

## Behavior

**Record then read round-trip**: recording a terminal or filesystem invocation persists it;
reading that tool type's history returns everything recorded for the session, oldest first.

**Cross-tool chronological ordering**: recording a terminal invocation, then a filesystem
invocation, then another terminal invocation assigns `seq` 1, 2, 3 respectively (shared counter)
— `read_terminal_history` returns the two terminal records with their original (non-contiguous)
`seq` values, preserving their position in the overall timeline.

**Terminal continuity**: after recording terminal invocations with different `cwd`/`env` values,
`read_terminal_state` reflects only the most recent one's `cwd`, `env`, and
`running_processes`, tagged with the `seq` it came from (`as_of_seq`).

**No terminal invocations yet**: `read_terminal_state` on a session with only filesystem
invocations (or no invocations at all) returns `None`, not an error.

**Edge cases**:

- Any of the five methods against an unknown `session_id` → `SessionNotFoundError`.
- `record_terminal_invocation` with `env=None` / `running_processes=None` stores empty
  defaults (`{}` / `[]`), not `None`, so `read_terminal_state` never has to handle a missing
  `env`/`running_processes` on an existing record.
- `read_terminal_history` / `read_filesystem_history` on a session with no invocations of that
  type returns an empty list, not an error.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-tool-state-abstraction.feature`.

## Related

- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M2 — tool state abstraction)
- Spec: [session-manager-core](20260717-session-manager-core.md) (the `session_id` this module keys off)
- Spec: [workspace-state-capture](20260717-workspace-state-capture.md) (the sibling passive-store module
  this one mirrors architecturally)
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md)
- Acceptance: `docs/specs/features/20260717-tool-state-abstraction.feature`

## Open Questions

- Other tool types named in the orchestrator concept doc (git, docker, kubectl, PostgreSQL, MCP)
  are out of scope until a concrete use case needs them; the `tool_id` discriminator and shared
  `tool_invocations` table are designed to accept a new tool type as an additive schema change
  (new `tool_id` value + new dataclass), not a redesign.
- Whether invocation history needs the same size-bounding/pruning treatment as workspace diffs
  (#5) is deferred until a session's tool history is observed to grow large in practice.
