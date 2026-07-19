# Tool State Abstraction Plan

## Status

draft

## Related

- Spec: [tool-state-abstraction](../specs/20260717-tool-state-abstraction.md)
- Depends on: [session-manager-core](../specs/20260717-session-manager-core.md) (`SessionNotFoundError`,
  the shared `sessions` table)
- Mirrors: [workspace-state-capture](../specs/20260717-workspace-state-capture.md) (same passive-store
  architecture)
- Issue: #7 (Create model-independent tool state layer, M2)

## Scope

Build `ToolStateManager`: a passive, per-session invocation history for terminal and filesystem
tool actions, ordered by one shared chronological sequence, with a derived "current terminal
state" view for continuity across model switches.

Out of scope for this plan (per the spec's Open Questions):

- Any tool type beyond terminal and filesystem (git, docker, kubectl, PostgreSQL, MCP).
- Active process/filesystem management by the orchestrator itself.
- Size-bounding/pruning of invocation history (revisit if it's observed to grow large).

## Steps

1. **Data model additions** (`src/llm_home_lab/state/models.py`) — `TerminalInvocation`,
   `FilesystemInvocation`, `TerminalState` dataclasses, reusing `SessionNotFoundError`.
2. **`ToolStateStore`** (`src/llm_home_lab/state/tool_state_store.py`, subclassing
   `SqliteStore`) — one `tool_invocations` table (`session_id`, `tool_id`, `seq` shared across
   tool types, JSON `payload`, `created_at`); `record_terminal`, `record_filesystem`,
   `read_terminal_history`, `read_filesystem_history`, `read_terminal_state` (derived from the
   highest-`seq` terminal row).
3. **`ToolStateManager`** (`src/llm_home_lab/state/tool_state_manager.py`) — the public async
   API, wrapping `ToolStateStore` via `asyncio.to_thread`, mirroring `WorkspaceManager`'s shape;
   `ToolStateManager.from_env()` reads `SESSION_STORE_PATH` (same shared store file).
4. **Package wiring** — export `ToolStateManager`, `TerminalInvocation`, `FilesystemInvocation`,
   `TerminalState` from `src/llm_home_lab/state/__init__.py`.
5. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
   .`, `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill): one test from the spec's behavior/acceptance scenarios at a
time, red → green, then a refactor pass once all scenarios pass.

## Risks

- **Shared-counter ordering bug**: since `seq` is shared across tool types, an off-by-one in how
  the next `seq` is computed (e.g. querying `MAX(seq)` scoped to the wrong table/tool_id) would
  silently break chronological ordering between terminal and filesystem rows. Cover this
  explicitly with the interleaving test from the spec, not just per-tool-type round-trips.
- **`read_terminal_state` staleness**: since it derives from "most recent terminal invocation"
  rather than a separately maintained field, it's automatically consistent with history by
  construction — but any future write path that bypasses `record_terminal_invocation` (e.g. a
  bulk import) would need to preserve `seq` ordering to keep this correct.

## Open Questions

- Same as the spec's: other tool types and history pruning are deferred, not blocking this plan.
