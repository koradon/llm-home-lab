# Session Manager Core

## Status

draft

## Summary

An internal, model-independent state layer that persists conversation history per session so
that orchestrator restarts and backend/model switches never lose context. This issue delivers
the `SessionManager` module itself — data model, persistence, and API — without wiring it into
the `/v1/chat/completions` gateway yet; that integration is a separate, later decision.

## User stories

- As the orchestrator, I want to persist a session's messages independently of any single model
  backend, so that switching backends mid-conversation doesn't lose history.
- As the orchestrator, I want session state to survive a process restart, so that an outage
  doesn't discard in-flight conversations.
- As a future orchestration component, I want to store a pre-computed summary against a session
  and reclaim the space of the messages it covers, so that token usage stays bounded over long
  conversations.

## Requirements

- Provide a `SessionManager` with an async API: `create_session`, `append_message`,
  `read_session`, `summarize`, `trim`.
- Persist sessions, messages, and summaries in SQLite (see
  [ADR-0002](../adr/0002-sqlite-for-session-storage.md)), at a configurable file path
  (`SESSION_STORE_PATH`, default `./data/sessions.db`).
- Messages are ordered within a session by a per-session monotonic `seq`, independent of any
  other session's ordering.
- `summarize` accepts a caller-supplied summary text and the highest message `seq` it covers; the
  session manager does not call any model backend itself.
- `read_session` returns the latest summary (if any) plus only the messages with `seq` greater
  than that summary's `covers_up_to_seq`.
- `trim` deletes messages covered by the latest summary and returns the number of messages
  deleted; it is a no-op when no summary exists.
- Unknown session ids raise `SessionNotFoundError` from `append_message`, `read_session`,
  `summarize`, and `trim`.
- An out-of-range `covers_up_to_seq` (greater than the session's highest message `seq`) raises
  `InvalidSummaryError` from `summarize`.
- Session state must be readable by a newly constructed `SessionManager` pointed at the same
  store path, with no explicit restore step, after the process that wrote it has exited.

## Behavior

**Create, append, read round-trip**: `create_session()` returns a new session id with no
messages and no summary. `append_message` adds messages in call order, each assigned the next
`seq`. `read_session` returns them in `seq` order with `summary = None`.

**Summarize then trim**: `summarize(session_id, summary_text, covers_up_to_seq)` stores a new
latest summary. Subsequent `read_session` calls return this summary plus only messages with
`seq > covers_up_to_seq`. `trim(session_id)` then deletes the covered messages from storage and
returns the count deleted; a following `read_session` is unaffected (those messages were already
excluded), but storage size is reduced.

**Trim with no summary**: `trim` on a session with no summary deletes nothing and returns `0`.

**Restart persistence**: a `SessionManager` instance A appends messages and summarizes a
session, then is discarded (simulating a process restart). A new `SessionManager` instance B,
constructed with the same `SESSION_STORE_PATH`, reads the same session and sees identical state.

**Edge cases**:

- `append_message` / `read_session` / `summarize` / `trim` against an unknown `session_id` →
  `SessionNotFoundError`, no partial writes.
- `summarize` with `covers_up_to_seq` greater than the session's current highest message `seq` →
  `InvalidSummaryError`, no summary is stored.
- Two sessions created independently never share or collide on `seq` values.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-session-manager-core.feature`.

## Related

- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M2 — session manager core)
- ADR: [0002-sqlite-for-session-storage](../adr/0002-sqlite-for-session-storage.md)
- Acceptance: `docs/specs/features/20260717-session-manager-core.feature`

## Open Questions

- When (and via what contract — request field, header, or separate endpoint) the gateway will
  adopt `session_id` is deferred to a later issue; this spec only covers the standalone module.
- Whether `decisions` and `constraints` (mentioned in the original milestone description) become
  first-class session fields is deferred until a concrete use case defines their shape.
