# Feed real completion outcomes into HealthMonitor, not just liveness probes

## Status

draft

## Problem

`HealthMonitor` ([failover-and-health-policy](../specs/20260717-failover-and-health-policy.md))
only ever receives `record_probe()` calls from the periodic background poller
(`_probe_all_hosts`), which calls each backend's `check_health()` —
`LMStudioBackend.check_health()` just hits `GET /v1/models` to confirm the LM Studio HTTP
server is up. It has no way to know whether the currently loaded model is actually producing
usable completions. A host can be fully reachable and pass every liveness probe while its model
burns its whole token budget on `reasoning_content` and returns empty/near-empty `content` for
every real `POST /v1/chat/completions` — and `HealthMonitor` never sees a single failure, so
`_eligible_candidates()` keeps routing real traffic to it indefinitely.

This isn't hypothetical: it's what happened overnight on 2026-07-26/27 while running the
facts-service curation-worker batch across the registered hosts. `GET /v1/nodes` showed 4 of 5
hosts "online" the whole time; `GET /metrics` showed availability at 75%; the
`availability-slo-burn` alert fired at 06:40 UTC and stayed firing. The operator only found the
likely-bad hosts (`localhost:1234`, `Lenovo Laptop`) by eyeballing torn/jagged load sparklines in
the TUI — a host that fails fast keeps re-acquiring and re-releasing its one
`max_concurrent_requests` slot, which looks spiky, but that's a coincidental read, not a real
diagnostic signal.

## Audience / Value

The home-lab operator (this project's own user), running unattended batch workloads (like
facts-service's curation worker) overnight against multiple registered LM Studio hosts. Right
now, a single degraded host silently eats a large fraction of a long run's throughput and the
only way to find it is manual, after the fact.

## Solution shape (not final)

Three parts, each usable independently, ordered by value-per-effort:

1. **Feed real completion outcomes into `HealthMonitor.record_probe()`.** In
   `chat_completions()` (`src/llm_home_lab/api/app.py`), right after a non-streaming
   `backend.complete(request)` returns, call `record_probe(decision.backend_id, healthy=<result
   isn't degenerate>, at=now)` — same call the poller already makes, just from a second source.
   "Degenerate" = empty/near-empty `content`, or a `finish_reason` other than `"stop"`. The
   streaming path (`_stream_chunks`) needs the same treatment once a stream finishes, or
   streaming callers stay invisible to it. No changes needed to `HealthMonitor` itself — its
   failure-threshold/cooldown/recovery state machine already does the right thing once it's fed
   real signal.
2. **Surface it.** Add a per-host failure count (or the last few `HealthMonitor.events`
   `FailoverEvent`s) to `GET /v1/nodes`, and a matching column in the TUI's Nodes table
   (alongside the existing host_id/status/ext_load/backend_type/in_flight/max/last_seen
   columns) — see [operator-observability-dashboards](operator-observability-dashboards.md) and
   the shipped [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md) spec, which
   this extends rather than replaces.
3. **(Optional, lower priority) Stop rewarding "fails fast" in routing.** Under the current
   least-loaded metric (`in_flight ÷ max_concurrent_requests`, `_eligible_candidates()`), a host
   that fails instantly frees its slot instantly too, so it can look chronically least-loaded and
   get preferentially routed to — worsening a degrading host's impact even before it crosses the
   failure threshold. `HealthMonitor.health_score()` already computes a rolling quality signal
   per backend but the spec explicitly scopes it as "a metrics/observability signal, not what
   drives the exclusion decision" — folding it into candidate ranking would be a deliberate,
   separate change to that scope, not a bug fix. Worth doing only if (1) and (2) turn out not to
   be enough in practice.

## Options

- **Option A — Do (1) only.** Smallest change, closes the actual correctness gap (a bad host
  gets excluded after `failure_threshold` bad completions, same as any other failure mode
  `HealthMonitor` already handles). Leaves the operator without an easy way to see *why* a host
  went unhealthy without reading logs.
- **Option B — (1) + (2).** Adds the visibility the operator actually asked for after this
  incident ("how would we know exactly which node is failing"). Small additional surface: one
  new field on `GET /v1/nodes`, one new TUI column.
- **Option C — (1) + (2) + (3).** Also changes routing behavior, not just exclusion — more
  design work (what weighting, how it interacts with `latency_ms`/load_ratio scoring in
  `RoutingCandidate`) and its own test surface. Treat as a separate follow-up decision once (1)
  and (2) are in and have been observed for a while.

## Constraints / Appetite

Home-lab scale, single maintainer — favor the smallest change that closes the actual gap.
Options A/B reuse `HealthMonitor`'s existing state machine untouched; no new persistence, no new
health mechanism. Option C is a real design change to routing and should not be bundled into the
same PR as A/B.

## Rabbit holes

- Picking a "degenerate completion" threshold that's too aggressive (e.g. flagging legitimately
  short-but-correct completions) and causing false-positive failovers. Start conservative (empty
  or whitespace-only content, or `finish_reason` explicitly indicating truncation/error) rather
  than a length heuristic.
- The streaming path is easy to forget — `_stream_chunks` is a separate code path from
  `backend.complete()` and doesn't currently touch `health_monitor` at all. If only the
  non-streaming path is fixed, streaming callers keep the exact blind spot this idea is about.
- Conflating this with Option C's routing-weight change — that's a bigger, separate decision
  (see [Open Questions](#open-questions)) and shouldn't block shipping A/B.

## No-gos

- No new health-check mechanism (e.g. a synthetic periodic "real" completion probe) — reusing
  real request traffic as the signal is cheaper and exercises the actual failure mode.
- No change to facts-service or any other client of the orchestrator — per
  [ADR-0013 in facts-service](../../../tripper/facts-service/docs/adr/0013-orchestrator-endpoint-is-worker-config-not-task-arg.md),
  clients are deliberately blind to backend placement; this gap is entirely the orchestrator's
  to close.

## Related

- Spec: [failover-and-health-policy](../specs/20260717-failover-and-health-policy.md) —
  `HealthMonitor`'s existing state machine and `health_score()`, both reused as-is by this idea
- ADR: [0006-background-health-poller](../adr/0006-background-health-poller.md) — establishes the
  periodic liveness probe this idea adds a second, request-driven signal alongside
- Idea: [operator-observability-dashboards](operator-observability-dashboards.md) — the TUI/API
  surface part (2) extends
- Spec: [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md) — Nodes table this
  idea would add an `errors` column to
- Module: `src/llm_home_lab/api/app.py` (`chat_completions`, `_stream_chunks`,
  `_eligible_candidates`), `src/llm_home_lab/health/monitor.py`,
  `src/llm_home_lab/backends/lmstudio.py` (`check_health`)
- Issue: #50 — feed real completion outcomes into `HealthMonitor` (Option A)
- Issue: #51 — surface per-host failure counts in `/v1/nodes` and the TUI (Option B)
- Issue: #52 — factor failure rate into routing (Option C, optional/lower priority)

## Open Questions

- What's the right "degenerate completion" definition — empty content only, or also very short
  content / non-`stop` finish reasons? Needs a decision before implementing (1).
- Should Option C (folding `health_score()` into routing weight) get its own idea/spec once A/B
  have shipped and been observed, or is it premature to even scope now? Leaning toward: revisit
  only if A/B don't fully resolve the "bad host hogs traffic" pattern in practice.
