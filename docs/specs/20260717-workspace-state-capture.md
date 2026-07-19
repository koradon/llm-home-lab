# Workspace State Capture Pipeline

## Status

draft

## Summary

A passive, per-session snapshot store for the caller-supplied facts about a coding workspace —
current git branch, diff, open files, and test status — so that switching models mid-task never
loses the surrounding development context. The orchestrator does not inspect any filesystem or
run git itself; per the [orchestrator concept](../../Local_LLM_Orchestrator_Concept.md), the
actual repo access lives on the agent's side (e.g. OpenCode). This module only normalizes,
bounds, and persists what it's given.

## User stories

- As the agent talking to the orchestrator, I want to hand over the current branch, diff, open
  files, and test status for a session, so that a different model picking up the same session
  has the same working context.
- As the orchestrator, I want large diffs or file lists bounded to a predictable size, so that a
  workspace snapshot never blows up prompt assembly or storage.
- As the orchestrator, I want a workspace snapshot to always belong to an existing session, so
  that workspace state and conversation state never drift apart.

## Requirements

- Provide a `WorkspaceManager` with an async API: `capture`, `read`.
- Persist snapshots in the same SQLite file as session state (see
  [ADR-0002](../adr/0002-sqlite-for-session-storage.md)), in a new `workspace_snapshots` table.
- A snapshot is keyed by `session_id`. Exactly one snapshot exists per session at a time —
  `capture` replaces whatever was there before.
- `capture(session_id, branch, git_diff, open_files, test_status=None)` raises
  `SessionNotFoundError` if `session_id` does not exist in the `sessions` table.
- `read(session_id)` raises `SessionNotFoundError` if `session_id` does not exist; returns `None`
  if the session exists but nothing has been captured for it yet.
- `git_diff` longer than `WORKSPACE_DIFF_MAX_CHARS` (default `20000`) is truncated to that length
  with a trailing marker noting how many characters were cut; the returned snapshot's
  `diff_truncated` flag is `True` in that case.
- `open_files` longer than `WORKSPACE_OPEN_FILES_MAX` (default `200`) entries is truncated to
  that count; the returned snapshot's `open_files_truncated` flag is `True` in that case.
- `test_status`, when supplied, is a structured record: `passed`, `failed`, `total` counts plus
  an optional free-text `summary`.

## Behavior

**Capture then read round-trip**: `capture` stores branch, diff, open files, and (optionally)
test status against an existing session. `read` returns exactly what was captured, plus
`created_at` and the two truncation flags (both `False` when nothing was cut).

**Re-capture replaces**: calling `capture` again for the same session overwrites the previous
snapshot entirely — `read` only ever returns the latest one.

**No snapshot yet**: `read` on a session that exists but has never been captured returns `None`,
not an error — this is a valid, expected state (a fresh session before any workspace context has
been sent).

**Oversized diff**: a `git_diff` longer than `WORKSPACE_DIFF_MAX_CHARS` is truncated to that
length with a trailing marker (e.g. `"... [truncated N chars]"`); `diff_truncated` is `True` on
read. The snapshot is still stored and returned — capture never rejects oversized input.

**Oversized open files list**: analogous to the diff — truncated to
`WORKSPACE_OPEN_FILES_MAX` entries, with `open_files_truncated` set to `True` on read.

**Edge cases**:

- `capture` / `read` against an unknown `session_id` → `SessionNotFoundError`.
- `capture` with an empty `git_diff` or empty `open_files` list is valid (e.g. a clean working
  tree) — not an error, `diff_truncated` / `open_files_truncated` are `False`.
- `test_status=None` is valid (caller has no test results to report yet); `read` reflects that
  as `test_status is None` rather than a zeroed-out record.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-workspace-state-capture.feature`.

## Related

- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M2 — workspace state capture)
- Spec: [session-manager-core](20260717-session-manager-core.md) (the `session_id` this module keys off)
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md)
- Acceptance: `docs/specs/features/20260717-workspace-state-capture.feature`

## Open Questions

- Whether/when the gateway or a future orchestration layer actually calls `capture` (e.g. after
  each agent turn) is deferred — this spec only covers the storage module, matching the scoping
  decision made for session manager core (#4).
- Whether `WORKSPACE_DIFF_MAX_CHARS` / `WORKSPACE_OPEN_FILES_MAX` need to be per-session
  configurable (vs. one global default) is deferred until a concrete case needs it.
