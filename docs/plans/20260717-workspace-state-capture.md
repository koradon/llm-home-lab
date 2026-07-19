# Workspace State Capture Plan

## Status

completed

## Related

- Spec: [workspace-state-capture](../specs/20260717-workspace-state-capture.md)
- Depends on: [session-manager-core](../specs/20260717-session-manager-core.md) (`SessionNotFoundError`,
  the `sessions` table a snapshot's `session_id` must exist in)
- Issue: #5 (Build workspace state capture pipeline, M2)

## Scope

Build `WorkspaceManager`: a passive, per-session snapshot store for caller-supplied workspace
facts (branch, diff, open files, test status), bounded to a predictable size, persisted in the
same SQLite file as session state.

Out of scope for this plan (per the spec's Open Questions):

- Any active git/filesystem inspection by the orchestrator — the module never runs git itself.
- Wiring `capture` into the gateway or any future orchestration loop.
- Per-session-configurable size limits (global env-var defaults only for now).

## Steps

1. **Data model additions** (`src/llm_home_lab/state/models.py`) — `RunStatus` and
   `WorkspaceSnapshot` dataclasses, reusing the existing `SessionNotFoundError`.
2. **`WorkspaceStore`** (`src/llm_home_lab/state/workspace_store.py`) — synchronous SQLite
   persistence: a `workspace_snapshots` table (one row per `session_id`, upserted on capture),
   `capture` / `read`, both checking the `sessions` table for existence first (reusing the same
   `sessions` table `SessionStore` already creates).
3. **Size bounding** — `capture` truncates `git_diff` past `WORKSPACE_DIFF_MAX_CHARS` (default
   `20000`) with a trailing marker, and `open_files` past `WORKSPACE_OPEN_FILES_MAX` (default
   `200`) entries, setting `diff_truncated` / `open_files_truncated` accordingly.
4. **`WorkspaceManager`** (`src/llm_home_lab/state/workspace_manager.py`) — the public async API,
   wrapping `WorkspaceStore` via `asyncio.to_thread`, mirroring `SessionManager`'s shape;
   `WorkspaceManager.from_env()` reads `SESSION_STORE_PATH` (same store path as sessions —
   they share one file) plus the two truncation env vars.
5. **Package wiring** — export `WorkspaceManager`, `WorkspaceSnapshot`, `RunStatus` from
   `src/llm_home_lab/state/__init__.py`.
6. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
   .`, `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill): one test from the spec's behavior/acceptance scenarios at a
time, red → green, then a refactor pass once all scenarios pass.

## Risks

- **Cross-table coupling**: `WorkspaceStore` reads the `sessions` table that `SessionStore` owns.
  Both stay in the same SQLite file by design (ADR-0002), so this is an intentional shared
  schema, not a layering violation — but it does mean the two stores must agree on the
  `sessions` table shape if it ever changes.
- **Truncation semantics drift**: diff and open-files truncation use the same "cut and flag"
  policy for consistency; if a future need requires different truncation behavior for one vs.
  the other (e.g. keep the *last* N files instead of the first N), that's a spec change, not
  just an implementation tweak.

## Open Questions

- Same as the spec's: when/how `capture` gets called in practice, and whether the size limits
  need to be per-session configurable — both deferred, not blocking this plan.
