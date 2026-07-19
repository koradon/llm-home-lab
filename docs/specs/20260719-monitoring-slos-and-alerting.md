# Monitoring, SLOs, and Alerting

## Status

draft

## Summary

An in-process `MetricsRegistry` that aggregates request outcomes, latency, failover success, host
saturation, and token usage into service-level indicators (SLIs); a `GET /metrics` endpoint that
exposes those SLIs in Prometheus text-exposition format so an operator's own Prometheus + Grafana
stack can build dashboards and retain history; and an `AlertEvaluator` that checks operator-defined
alert rules (SLO burn, latency threshold, saturation threshold) against those SLIs and logs
firing/resolving alerts, each carrying a link into a new `docs/runbooks/` doc. This follows the
codebase's existing pattern (`HealthMonitor`, `HostRegistry`): pure in-process state, no new
external dependency, caller-supplied `at` for deterministic tests — Prometheus/Grafana/Alertmanager
remain optional external consumers an operator wires up themselves, not something this repo runs or
persists data for.

## User stories

- As an operator, I want current and historical availability, latency, and saturation for the
  orchestrator, so that I can see degradation trends, not just point-in-time health.
- As an operator, I want the orchestrator to expose its metrics in a format my existing
  Prometheus/Grafana setup already understands, so that I don't have to build or run a bespoke
  dashboard.
- As an operator, I want to know when an SLO is burning down faster than sustainable, or when a
  host is saturated, so that I can act before it becomes an outage.
- As an operator responding to a critical alert, I want a runbook link attached to it, so that I
  know the likely cause and first mitigation step without reverse-engineering the system under
  pressure.
- As a test author, I want alert rules verifiable by feeding synthetic metric sequences with
  caller-supplied timestamps, so that "this alert fires under this incident shape" is testable
  without a real Prometheus/Alertmanager instance.

## Requirements

- Add `llm_home_lab/observability/` with:
  - `models.py` — `AlertSeverity` (`"critical" | "warning"`), `AlertRule` (`name`, `kind`:
    `"threshold" | "slo_burn"`, `metric`, `comparison`, `threshold_value`, `window: timedelta`,
    `severity`, `runbook_url`), `AlertEvent` (`rule_name`, `severity`, `state`: `"firing" |
    "resolved"`, `value`, `threshold_value`, `runbook_url`, `at`), `SliSnapshot` (`availability`,
    `p95_latency_ms`, `failover_success_rate`, `host_saturation: dict[str, float]`, `queue_depth`,
    `token_usage_total: dict[str, int]`).
  - `metrics.py` — `MetricsRegistry`, a pure in-process aggregator matching `HealthMonitor`'s
    determinism convention (every method that reads "now" takes `at` from the caller):
    - `record_request(endpoint: str, status_code: int, latency_ms: float, at: datetime)` — called
      alongside the existing `access_logger` line in `log_requests` middleware. Maintains a
      rolling window (default 5 minutes, matching this codebase's existing small, fixed defaults
      like `HealthMonitor`'s cooldown) of `(status_code, latency_ms, at)` samples per endpoint,
      evicting samples older than the window on each call.
    - `record_failover_outcome(succeeded: bool, at: datetime)` — called from `chat_completions`
      whenever at least one model-capable host was excluded from candidates due to health (i.e. a
      failover was in play for this request), recording whether the request still succeeded.
    - `record_token_usage(host_id: str, prompt_tokens: int, completion_tokens: int, at: datetime)`
      — called after a successful chat completion; accumulates a running total per `host_id`
      (all-time counters, not windowed — token spend is cumulative, not a rate to window).
    - `snapshot(at: datetime, registry: HostRegistry, scheduling_queue: SchedulingQueue) ->
      SliSnapshot` — computes availability (fraction of requests in the rolling window with
      status `< 500`), p95 latency (from the same window's latency samples), failover success
      rate (successes / total recorded failover-involved requests in the window), per-host
      saturation (`in_flight / max_concurrent_requests` read live from `registry.hosts()`), queue
      depth (read live from `scheduling_queue`), and cumulative token usage per host.
    - `render_prometheus(at: datetime, registry: HostRegistry, scheduling_queue:
      SchedulingQueue) -> str` — renders a `SliSnapshot` as Prometheus text-exposition format
      (`# TYPE`/`# HELP` lines, gauges for availability/latency/saturation/queue depth, counters
      for token usage), content-type `text/plain; version=0.0.4`.
  - `alerts.py` — `AlertEvaluator(rules: list[AlertRule])`:
    - `evaluate(snapshot: SliSnapshot, at: datetime) -> list[AlertEvent]` — a pure function of the
      snapshot, the configured rules, and `at`; no wall-clock or I/O. For each rule, compares the
      relevant `SliSnapshot` field against `threshold_value` per `comparison`. A `"threshold"`
      rule fires when the current value crosses the threshold. A `"slo_burn"` rule fires when the
      *error rate* implied by the relevant SLI (e.g. `1 - availability`) exceeds the burn rate
      implied by `threshold_value` (the SLO target, e.g. `0.99`) over `window`.
    - Mirrors `HealthMonitor._record_transition`: emits an `AlertEvent` only on a firing/resolved
      *transition*, not on every evaluation, to avoid log spam from a steadily-firing alert. Each
      transition is logged via a new `llm_home_lab.alerts` logger: `rule=%s severity=%s state=%s
      value=%s threshold=%s runbook=%s`.
  - Alert rules are loaded once at startup from a JSON file (path via
    `ORCHESTRATOR_ALERT_RULES_FILE`, default `./config/alert_rules.json`), matching this
    codebase's existing read-env-var-at-startup, no-hot-reload, JSON-config convention
    (`ApiKeyStore.from_file`/`config/api_keys.json`) rather than introducing a new YAML-parsing
    dependency. Missing file at startup → an empty rule list (metrics/`/metrics` still work;
    nothing alerts) rather than a crash, matching `_load_key_store()`'s missing-file behavior.
  - Ship a default `config/alert_rules.json` covering the SLIs named in the issue: an
    `slo_burn` rule on availability (target `0.99` over a rolling window), a `threshold` rule on
    p95 latency, and a `threshold` rule on host saturation (`in_flight / max_concurrent_requests`
    approaching `1.0`) — each `critical`-severity rule carries a `runbook_url` into
    `docs/runbooks/`.
- `create_app` gains a `metrics_registry: MetricsRegistry` and an `alert_evaluator:
  AlertEvaluator` parameter (both required, no silent default, matching this codebase's
  established pattern of required collaborators over hidden globals — see `key_store`,
  `scheduling_queue`).
- `SchedulingQueue` gains a `depth() -> int` accessor (total entries queued across every tier and
  session) — today nothing outside the queue can read its size, which the queue-depth SLI needs.
- New endpoint `GET /metrics`: returns `MetricsRegistry.render_prometheus(...)`, `text/plain;
  version=0.0.4`. Exempt from auth like `/health/live`/`/health/ready` (a scrape target, not a
  client-data endpoint) — added to `AUTH_EXEMPT_PATHS`.
- New endpoint `GET /v1/alerts`: JSON diagnostic listing of currently-firing alerts (mirrors the
  `/v1/nodes` diagnostic style) — `{"alerts": [{"rule": ..., "severity": ..., "state": "firing",
  "value": ..., "threshold": ..., "runbook_url": ...}, ...]}`. Authenticated like `/v1/nodes`
  (operational data, not a scrape target).
- Add `docs/runbooks/` with one short runbook per critical default rule (symptom, likely cause,
  first mitigation step) — a new top-level doc folder per `docs/README.md`'s growth model; update
  `docs/README.md`'s structure table when these are created.

## Behavior

**Metrics reflect a rolling window, not all-time, except token usage**: availability, p95 latency,
and failover success rate are computed over the most recent window (default 5 minutes) of
recorded requests — a burst of errors ages out of the window and stops affecting the SLI once
old enough. Token usage counters are cumulative totals per host, since operators want total spend,
not a rate.

**`/metrics` always returns 200, even with zero rules configured or zero traffic recorded**:
absence of data renders as zero-valued gauges/counters, not an error — a fresh orchestrator with no
requests yet is still scrapable.

**An alert transitions from resolved to firing exactly once per breach, not on every scrape**: if
p95 latency stays above threshold across ten consecutive evaluations, exactly one `firing`
`AlertEvent` is logged (on the first breach), not ten; the next logged event is the `resolved`
transition once the value recovers.

**A `slo_burn` rule and a `threshold` rule on the same metric are independent**: they evaluate and
transition separately, so a deployment can have both a fast-reacting latency threshold alert and a
slower-burning availability SLO alert without one suppressing the other.

**Failover success rate is only computed from requests where a failover was actually in play**: a
request served by the very first candidate (no host excluded for health) does not count toward
this SLI at all — it is not "a success," it's not applicable, keeping the denominator meaningful
(this mirrors why `HealthMonitor.is_healthy` and this SLI are separate concerns: one is per-host
state, the other is per-request outcome).

**Queue depth and host saturation are read live, not sampled into the rolling window**: unlike
latency/availability (which are about *what happened* recently), saturation and queue depth are
*current state*, read directly from `HostRegistry`/`SchedulingQueue` at snapshot time — a spike
that already drained is not still reported.

**Missing or malformed alert rules file fails safe, not silently wrong**: a missing file yields no
rules (documented in Related); a malformed file (invalid JSON, unknown `kind`) raises at startup
rather than silently loading a partial or misinterpreted rule set — consistent with this codebase
preferring a loud startup failure over a subtly wrong running state.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/20260719-monitoring-slos-and-alerting.feature`.

## Related

- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md) — the
  TUI/web-dashboard idea this spec's `/metrics` endpoint is meant to feed; this spec only covers
  the shared metrics/alerting backend, not either front-end.
- Module: [`health/monitor.py`](../../src/llm_home_lab/health/monitor.py) — `HealthMonitor`'s
  caller-supplied-`at`, transition-only-logging pattern this spec's `MetricsRegistry`/
  `AlertEvaluator` both follow.
- Module: [`api/app.py`](../../src/llm_home_lab/api/app.py) — `log_requests` middleware
  (`record_request` hooks in here), `chat_completions` (`record_failover_outcome`/
  `record_token_usage` hook in here), `AUTH_EXEMPT_PATHS` (`/metrics` joins `/health/live`/
  `/health/ready`).
- Module: [`scheduling/queue.py`](../../src/llm_home_lab/scheduling/queue.py) — gains `depth()`.
- Spec: [multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md) —
  `HostRegistry`/`SchedulingQueue` this spec reads saturation and queue depth from.
- Spec: [security-and-governance-baseline](20260717-security-and-governance-baseline.md) —
  `AUTH_EXEMPT_PATHS` convention `/metrics` follows; `/v1/alerts` stays authenticated like
  `/v1/nodes`.
- Plan: (to be written) `docs/plans/20260719-monitoring-slos-and-alerting.md`
- Issue: [#12](https://github.com/koradon/llm-home-lab/issues/12) — Add monitoring, SLOs, and
  alerting (milestone M4)
- Acceptance: `docs/specs/features/20260719-monitoring-slos-and-alerting.feature`

## Open Questions

- Who calls `AlertEvaluator.evaluate` and on what cadence — piggy-backed on the `/health/ready`
  sweep (already runs periodically-ish via readiness probes) vs. a dedicated background task — is
  left to the plan, matching how the multi-node spec deferred "who calls `expire_stale`."
- Whether the rolling window (default 5 minutes) should be configurable per-deployment or fixed —
  deferred; fixed is simplest and matches this codebase's other fixed-with-override defaults
  (`HealthMonitor`'s cooldown, `heartbeat_ttl`).
- Whether `slo_burn` needs Google SRE-style multi-window (short + long window) burn-rate detection,
  or whether the single-window definition above is sufficient at home-lab scale/traffic — the
  single-window version is proposed here as the simpler starting point; revisit if it proves too
  noisy or too slow to detect real burn in practice.
- Exact Prometheus metric names/labels are left to the plan (not a wire contract stability
  concern the way the HTTP API is, since this is a new endpoint with no existing consumers yet).
