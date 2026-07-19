# Persist the node registry; stop auto-deregistering unreachable hosts

## Status

accepted

## Context and Problem Statement

`HostRegistry` ([multi-node-registry-and-scheduler](../specs/20260717-multi-node-registry-and-scheduler.md),
M4) is purely in-memory (`dict[str, HostInfo]`) and `expire_stale` removes a host once
`at - host.last_seen >= heartbeat_ttl` (default 60s). `expire_stale` runs on every
`GET /health/ready` call. In practice, testing a real multi-node setup (a Mac + a Windows LM
Studio host) showed this is disruptive: a host registered once vanishes from `/v1/nodes` within a
minute unless something calls `POST /v1/nodes/{host_id}/heartbeat` at least that often — nothing
in this repo does that today, so an operator manually re-registers a host every time they happen
to check on it. The registry also does not survive an orchestrator restart at all, since it is
never persisted.

The operator's position (this ADR's driving input): DHCP-induced IP churn is the operator's own
responsibility to solve (static IPs / DHCP reservations for LM Studio hosts), not something this
orchestrator should paper over by silently dropping registrations. A registered host should stay
registered until explicitly removed, across restarts. Reachability should be visible
(online/offline), not used to silently forget a host's existence.

## Considered Options

- **A — Keep heartbeat-based auto-deregistration, just relax the TTL.** Doesn't address the
  operator's actual objection (registration should not be time-bounded at all), and does nothing
  for the "wiped on restart" problem.
- **B — Persist `HostRegistry` to SQLite (reusing [ADR-0002](0002-sqlite-for-session-storage.md)'s
  engine choice) and stop calling `expire_stale` on a TTL at all.** A registered host is
  permanent until explicitly `DELETE`d. `HealthMonitor`'s existing healthy/unhealthy tracking
  (already separate from registration — see M3's failover spec) becomes the sole source of
  "online/offline," surfaced through the API and consumed by the TUI/web UI, instead of also
  controlling whether the host is known to exist at all.
- **C — Keep in-memory-only, add a config file for node definitions (like `api_keys.json`).**
  Simpler than a DB, but this project's other multi-row, queryable, updated-at-runtime state
  (sessions, workspace, tool state) already lives in SQLite; a node registry has the same shape
  (rows keyed by id, updated by heartbeat/health probes) and reusing the established pattern beats
  introducing a second persistence mechanism for one more entity.

## Decision Outcome

Chosen option: **B**. `HostRegistry` becomes SQLite-backed (new table, same database file or a
sibling one following `state/sqlite_base.py`'s `SqliteStore` pattern), so registrations survive an
orchestrator restart. `expire_stale`/`heartbeat_ttl` and the TTL-based removal are deleted
entirely — registration is permanent until `DELETE /v1/nodes/{host_id}` is called. `last_seen` is
still recorded (useful operational data) but no longer drives removal.

Reachability becomes purely `HealthMonitor`'s concern: `GET /v1/nodes` gains an online/offline
field sourced from `health_monitor.is_healthy(...)`, and the TUI (and the planned web UI) render
it per host instead of a host silently disappearing from the list. Routing behavior is unaffected
— `_eligible_candidates` already excludes unhealthy hosts; the only change is that an unhealthy
host stays *visible* (as offline) instead of vanishing from the registry.

Tracked as issues (not yet attached to a milestone): persist the registry to SQLite and remove
auto-deregistration; expose and display online/offline status. See `.plan/issues/`.

### Consequences

- Good, because a host registered once stays known until an operator explicitly removes it —
  matches the operator's expectation and stops registrations from silently evaporating during
  normal use (e.g., while nothing has called `/health/ready` in the last minute).
- Good, because the registry survives an orchestrator restart, consistent with this project's
  other state (sessions, workspace, tool state — all SQLite-backed and restart-durable).
- Good, because "is this host reachable" and "does this host exist" become independently visible
  (online/offline vs. registered/not), which is more honest than conflating them.
- Bad, because a genuinely retired host (decommissioned machine, changed IP with no DHCP
  reservation) now lingers in the registry forever unless someone remembers to `DELETE` it —
  accepted, since the operator explicitly prefers an always-visible-but-possibly-offline entry
  over a silently vanishing one.
- Bad, because `DELETE /v1/nodes/{host_id}` is the only removal path now, and it is currently
  broken for any `host_id` containing `/` (e.g., the default host registered from
  `LMSTUDIO_BASE_URL`, whose `host_id` is the full base URL) — tracked as its own issue; this
  ADR's design depends on that path actually working for every host_id shape in use.
- Neutral: the "which database file" question (same file as session state vs. a new one) is left
  to the implementation issue; either is consistent with this ADR, since both reuse the same
  engine.

- **Revisit trigger**: if operators start wanting bulk/scripted node definitions (e.g., a fleet of
  10+ hosts declared once at deploy time rather than registered via API calls one at a time),
  reconsider a declarative config file feeding the registry at startup, layered on top of (not
  instead of) the persisted registry.
