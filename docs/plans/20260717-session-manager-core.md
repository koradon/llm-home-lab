# Session Manager Core Plan

## Status

draft

## Related

- Spec: [session-manager-core](../specs/20260717-session-manager-core.md)
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md)
- Issue: #4 (Implement session manager core, M2)

## Scope

Build the `SessionManager` module described in the spec: a standalone, model-independent state
layer persisting session messages and summaries in SQLite, with its own async API
(`create_session`, `append_message`, `read_session`, `summarize`, `trim`). Session state must
survive an orchestrator restart.

Out of scope for this plan (deferred to later issues, per the spec's Open Questions):

- Wiring `session_id` into the `/v1/chat/completions` gateway.
- `decisions` / `constraints` as session fields.
- Workspace state capture (#5) and tool state abstraction (#7) — separate issues, though they
  will reuse this module's package layout and storage pattern.

## Steps

1. **Data model and exceptions** (`src/llm_home_lab/state/models.py`) — `StoredMessage`,
   `Summary`, `Session` dataclasses; `SessionError`, `SessionNotFoundError`,
   `InvalidSummaryError` exceptions. No I/O.
2. **`SessionStore`** (`src/llm_home_lab/state/store.py`) — synchronous SQLite persistence:
   schema (`sessions`, `messages`, `summaries` tables), one short-lived connection per call,
   `create_session` / `append_message` / `read_session`, each per-session `seq`-ordered and
   raising `SessionNotFoundError` for unknown ids.
3. **Summarize and trim on `SessionStore`** — `summarize` validates `covers_up_to_seq` against
   the session's highest message `seq` (raising `InvalidSummaryError` if it's out of range) and
   stores the caller-supplied summary text; `trim` deletes messages covered by the latest
   summary and returns the count deleted (a no-op returning `0` when there's no summary yet).
4. **`SessionManager`** (`src/llm_home_lab/state/session_manager.py`) — the public async API,
   wrapping `SessionStore` via `asyncio.to_thread` so it's safe to call from FastAPI handlers
   later without blocking the event loop; `SessionManager.from_env()` reads `SESSION_STORE_PATH`
   (default `./data/sessions.db`).
5. **Package wiring** — export the public surface from `src/llm_home_lab/state/__init__.py`;
   add `data/` to `.gitignore`.
6. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
   .`, `uv run ruff format --check .`, `uv run mypy src`.

Each step is implemented test-first (see the `tdd` skill): write the test from the spec's
behavior/acceptance scenarios, watch it fail, implement, watch it pass, commit.

## Risks

- **Schema churn**: the program plan flags M2's session/workspace/tool schemas as underpinning
  M3 routing/caching — changing the `messages`/`summaries` table shape later has downstream
  cost. Mitigation: the spec deliberately scopes this issue to messages + summaries only,
  deferring `decisions`/`constraints` until a concrete need defines their shape.
- **SQLite concurrency**: connection-per-call with WAL mode is simple and correct for this
  home-lab's single-process, low-concurrency use, but wouldn't hold up under heavier concurrent
  write load. Acceptable per ADR-0002; revisit only if a future milestone needs a networked or
  multi-process store.

## Open Questions

- Same as the spec's: when/how `session_id` enters the gateway contract, and whether
  `decisions`/`constraints` become first-class fields — both deferred, not blocking this plan.
