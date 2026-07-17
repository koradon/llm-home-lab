# Use SQLite for session state persistence

## Status

accepted

## Context and Problem Statement

M2 introduces an orchestrator-owned state layer (session, workspace, tool state) that must
survive process restarts and stay usable across model/backend switches (see
[orchestrator-program plan](../plans/orchestrator-program.md)). The program plan explicitly
flagged local storage choice — embedded DB vs. plain files — as a decision to make before M2
implementation, since the schema underpins later routing/caching work in M3. The session manager
core ([spec](../specs/session-manager-core.md)) is the first module that needs to make this
choice, for messages and summaries keyed by session id.

## Considered Options

- SQLite (Python stdlib `sqlite3`), one embedded database file.
- Plain JSON files, one file per session under a data directory.

## Decision Outcome

Chosen option: "SQLite", because sessions need per-session ordered messages plus a
replace-a-range-with-a-summary operation (`summarize` + `trim`), which maps directly onto SQL
row operations (`DELETE ... WHERE seq <= ?`) with atomicity guarantees that flat files don't give
for free. It requires no extra service to run — it stays true to the home-lab, single-process
deployment model — while giving room to add indexed queries (e.g. by session id) as the state
layer grows through workspace state (#5) and tool state (#7).

### Consequences

- Good, because trim/summarize operations are atomic single statements instead of hand-rolled
  read-modify-write-whole-file logic.
- Good, because the same database file can gain new tables for workspace and tool state in later
  M2 issues without introducing a second storage mechanism.
- Good, because no additional runtime dependency or service is introduced — `sqlite3` is stdlib.
- Bad, because the on-disk format is not human-readable for casual debugging, unlike JSON files.
- Bad, because schema changes need explicit migration handling as the state layer grows, whereas
  JSON files could add fields without any migration step.
- Mitigation: keep the schema minimal and additive for now (see the session manager core spec's
  deferred `decisions`/`constraints` fields); revisit if a later milestone needs a networked or
  multi-process store, which SQLite does not support well.
