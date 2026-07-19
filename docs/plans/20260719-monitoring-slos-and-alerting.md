# Monitoring, SLOs, and Alerting Plan

## Status

completed

## Related

- Spec: [monitoring-slos-and-alerting](../specs/20260719-monitoring-slos-and-alerting.md)
- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
- Depends on: [`health/monitor.py`](../../src/llm_home_lab/health/monitor.py) (`HealthMonitor`'s
  caller-supplied-`at`, transition-only-logging pattern this plan's `MetricsRegistry`/
  `AlertEvaluator` both copy)
- Depends on: [`api/app.py`](../../src/llm_home_lab/api/app.py) (`log_requests` middleware,
  `chat_completions`, `AUTH_EXEMPT_PATHS`, `/health/ready` — all touched here)
- Depends on: [`scheduling/queue.py`](../../src/llm_home_lab/scheduling/queue.py) (`depth()`
  addition)
- Depends on: [`security/key_store.py`](../../src/llm_home_lab/security/key_store.py)
  (`ApiKeyStore.from_file`'s JSON-loading-at-the-edge shape, mirrored by
  `AlertEvaluator.from_file`)
- Issue: #12 (Add monitoring, SLOs, and alerting, M4)

## Scope

Build `MetricsRegistry` and `AlertEvaluator` (`src/llm_home_lab/observability/`), wire them into
`create_app`, expose `GET /metrics` (Prometheus text format, auth-exempt) and `GET /v1/alerts`
(authenticated JSON diagnostic), ship a default `config/alert_rules.json`, and write one runbook
per default critical rule under `docs/runbooks/`.

Concrete decisions for this plan (resolving the spec's deferred wiring questions):

- **Alert evaluation cadence**: piggy-back on the existing `/health/ready` handler — the same
  place `registry.expire_stale` was added for #10 — rather than introducing a background poller.
  `/health/ready` already runs a health probe for every host on each call; add one
  `alert_evaluator.evaluate(metrics_registry.snapshot(now, registry, scheduling_queue), now)` call
  there. This means alert freshness is tied to how often something calls `/health/ready` (an
  external prober, cron, or manual check) — the same operational assumption `expire_stale` already
  makes. `GET /v1/alerts` and `GET /metrics` read the evaluator's/registry's *current* state on
  every call; they do not themselves trigger evaluation, so repeatedly hitting them between
  `/health/ready` calls is safe (idempotent reads) but won't surface a transition that hasn't been
  evaluated yet.
- **Rolling window**: fixed at 5 minutes, not configurable per-deployment (matches
  `HealthMonitor`'s fixed-with-constructor-override defaults). `MetricsRegistry.__init__` accepts
  a `window: timedelta = timedelta(minutes=5)` constructor param for tests to shrink it, the same
  way `HealthMonitor` exposes `cooldown` as a constructor param rather than a global constant.
- **`slo_burn` definition**: single-window (no short/long dual-window burn-rate detection). A
  `slo_burn` rule's `threshold_value` is the SLO target (e.g. `0.99` availability over `window`);
  it fires when `(1 - current_value) > (1 - threshold_value)`, i.e. the observed error rate over
  the rule's own window exceeds the error budget implied by the target. Simpler than Google
  SRE-workbook multi-window burn detection; flagged in the spec as revisit-if-noisy.
- **Prometheus rendering**: hand-rolled text formatting (`# HELP`/`# TYPE` + metric lines), not the
  `prometheus_client` library — avoids a new dependency for a handful of gauges/counters. Metric
  names: `llm_home_lab_availability_ratio`, `llm_home_lab_request_latency_p95_ms`,
  `llm_home_lab_failover_success_ratio`, `llm_home_lab_host_saturation_ratio{host_id="..."}`,
  `llm_home_lab_queue_depth`, `llm_home_lab_token_usage_total{host_id="..."}` (counter).

Out of scope for this plan (per the spec's Open Questions):

- Multi-window SLO burn-rate detection.
- Configurable rolling-window size via env var (constructor param only, for tests).
- The TUI/web-dashboard front ends from the idea doc — this plan only builds the backend they'd
  consume.

## Steps

1. **Observability package** (`src/llm_home_lab/observability/`, new package sibling to
   `registry/`, `scheduling/`, `security/`) — `__init__.py` exporting the public surface.
2. **Models** (`observability/models.py`) — `AlertSeverity` (str enum: `critical`, `warning`),
   `AlertRule` (`name: str`, `kind: Literal["threshold", "slo_burn"]`, `metric: str`,
   `comparison: Literal["gt", "lt"]`, `threshold_value: float`, `window: timedelta`,
   `severity: AlertSeverity`, `runbook_url: str`), `AlertEvent` (`rule_name`, `severity`,
   `state: Literal["firing", "resolved"]`, `value`, `threshold_value`, `runbook_url`, `at`),
   `SliSnapshot` (`availability: float`, `p95_latency_ms: float`, `failover_success_rate: float |
   None` — `None` when the denominator is zero, i.e. no failover-involved requests in the window,
   `host_saturation: dict[str, float]`, `queue_depth: int`, `token_usage_total: dict[str, int]`),
   and an `AlertRuleFileError` for malformed-file loading.
3. **`MetricsRegistry`** (`observability/metrics.py`):
   - `__init__(window: timedelta = timedelta(minutes=5))`.
   - `record_request(endpoint: str, status_code: int, latency_ms: float, at: datetime)` — appends
     to a per-`endpoint` `deque`, evicting samples older than `window` relative to `at` on every
     call (same eviction-on-write style as `HealthMonitor`'s bounded probe history, just
     time-bounded instead of count-bounded).
   - `record_failover_outcome(succeeded: bool, at: datetime)` — appends `(succeeded, at)` to its
     own rolling deque, evicted the same way.
   - `record_token_usage(host_id: str, prompt_tokens: int, completion_tokens: int, at: datetime)`
     — accumulates into `dict[str, int]`, all-time, no window/eviction.
   - `snapshot(at, registry: HostRegistry, scheduling_queue: SchedulingQueue) -> SliSnapshot` —
     evicts first, then computes: availability = `count(status < 500) / count(total)` across all
     endpoints' samples in-window (`1.0` if no samples — an idle orchestrator isn't unavailable);
     p95 latency via a simple sorted-list percentile over in-window latency samples (`0.0` if no
     samples); failover success rate = `succeeded_count / total_count` over the in-window
     failover-outcome deque, `None` if that deque is empty; host saturation and queue depth read
     live from `registry.hosts()` / `scheduling_queue.depth()` (not stored in `MetricsRegistry`
     itself).
   - `render_prometheus(at, registry, scheduling_queue) -> str` — calls `snapshot` internally,
     formats as described in Scope. `failover_success_rate=None` renders as omitting that metric
     line entirely for that scrape (Prometheus convention: absent, not `NaN`, when undefined).
4. **`AlertEvaluator`** (`observability/alerts.py`):
   - `__init__(rules: list[AlertRule])` — internal `_firing: dict[str, bool]` initialized to
     `False` per rule name.
   - `evaluate(snapshot: SliSnapshot, at: datetime) -> list[AlertEvent]` — for each rule, resolve
     `snapshot`'s field named by `rule.metric` (a small `metric -> SliSnapshot` field lookup;
     `host_saturation`/`token_usage_total` rules are evaluated once per key present in that dict,
     e.g. one potential event per `host_id`). Compute breach per `kind` (see Scope). Compare
     against `_firing[rule.name]` (or `_firing[f"{rule.name}:{host_id}"]` for per-host metrics):
     transition `False -> True` emits a `firing` `AlertEvent` and logs via the new
     `llm_home_lab.alerts` logger (`rule=%s severity=%s state=%s value=%.4f threshold=%.4f
     runbook=%s`); transition `True -> False` emits `resolved`; no transition emits nothing. A
     metric with no data this snapshot (e.g. `failover_success_rate=None`) is skipped for that
     evaluation — neither fires nor resolves.
   - `AlertEvaluator.from_file(path: str) -> AlertEvaluator` classmethod — JSON parsing at the
     edge (mirrors `ApiKeyStore.from_file`), raising `AlertRuleFileError` on an unknown `kind` or
     malformed JSON structure.
   - `current_state() -> list[AlertEvent]` — the latest known state per rule (for `GET
     /v1/alerts`), not a re-evaluation; sourced from the last `AlertEvent` produced per rule
     (`firing` ones only, since `resolved` means it's no longer active) plus rules never yet
     breached are simply absent from the list.
5. **`SchedulingQueue.depth()`** (`scheduling/queue.py`) — sum of `len(deque)` across every
   session deque in every priority tier. Trivial, test first in isolation.
6. **Default config** (`config/alert_rules.json`, `.gitignore`d like `api_keys.json`? — no, unlike
   API keys this file has no secrets, so it is committed, giving new clones a working default) —
   three rules: `availability-slo-burn` (`slo_burn`, `metric="availability"`,
   `threshold_value=0.99`, `window=5m`, `critical`), `p95-latency-threshold` (`threshold`,
   `metric="p95_latency_ms"`, `comparison="gt"`, a starting threshold like `5000`, `warning`),
   `host-saturation-threshold` (`threshold`, `metric="host_saturation"`, `comparison="gt"`,
   `threshold_value=0.9`, `critical`). Exact latency/saturation thresholds are starting guesses an
   operator is expected to tune — noted in the runbook docs, not treated as a stable contract.
7. **Runbooks** (`docs/runbooks/`, new top-level doc folder) — `availability-slo-burn.md`,
   `host-saturation-threshold.md` (both `critical`, so both need a `runbook_url` per the spec);
   each: symptom, likely cause, first mitigation step. Update `docs/README.md`'s structure table
   with the new `docs/runbooks/` row, per `DECISION_RULES.md`'s "extending the documentation tree"
   step.
8. **App wiring** (`src/llm_home_lab/api/app.py`):
   - `create_app` gains required `metrics_registry: MetricsRegistry` and `alert_evaluator:
     AlertEvaluator` parameters.
   - `log_requests` middleware additionally calls `metrics_registry.record_request(request.url.path,
     response.status_code, latency_ms, datetime.now(UTC))` right after computing `latency_ms`.
   - `chat_completions`: after computing `candidates` (pre-dispatch), if `len(candidates) <
     len(model_hosts)` a failover is in play for this request; wrap the dispatch/response path so
     that on success `metrics_registry.record_failover_outcome(True, now)` is recorded and on a
     `NoAvailableBackendError`/backend error `record_failover_outcome(False, now)` is recorded —
     only when that flag was set for this request. Also, on a successful (non-streaming) response,
     call `metrics_registry.record_token_usage(decision.backend_id, result.prompt_tokens,
     result.completion_tokens, now)`. Streaming responses do not currently expose token counts
     (the existing `_stream_chunks` path has no usage payload) — token usage recording is scoped to
     non-streaming completions only for this plan; flagged as a Risk below.
   - `AUTH_EXEMPT_PATHS` gains `/metrics`.
   - New `GET /metrics` endpoint — `Response(content=metrics_registry.render_prometheus(now,
     registry, scheduling_queue), media_type="text/plain; version=0.0.4")`.
   - New `GET /v1/alerts` endpoint (authenticated, like `/v1/nodes`) — returns
     `{"alerts": [asdict(event) for event in alert_evaluator.current_state()]}`.
   - `/health/ready` handler additionally calls
     `alert_evaluator.evaluate(metrics_registry.snapshot(now, registry, scheduling_queue), now)`
     after its existing per-host probe loop.
9. **Entry point** (`src/llm_home_lab/main.py`) — `create_default_app` constructs one shared
   `MetricsRegistry()` and loads
   `AlertEvaluator.from_file(os.environ.get("ORCHESTRATOR_ALERT_RULES_FILE",
   "./config/alert_rules.json"))` if the file exists, else `AlertEvaluator([])` (matching
   `_load_key_store`'s missing-file-is-not-a-crash convention), passed to `create_app`.
10. **Existing test updates** — every `create_app(...)` call site (`test_gateway.py`,
    `test_health.py`, `test_failover.py`, `test_node_registry_endpoints.py`,
    `test_capacity_scheduling.py`, `test_auth_middleware.py`, `test_model_availability.py`,
    `test_main.py`) needs a `metrics_registry`/`alert_evaluator` addition — a shared
    `_metrics_registry()` / `_alert_evaluator()` helper per test module (empty rules list is a
    valid, permissive default), matching the `_key_store()` helper pattern from #11.
11. **New tests**: `test_metrics_registry.py` (rolling window eviction, availability/p95/failover
    computation including the `None`-denominator case, token usage accumulation),
    `test_alert_evaluator.py` (threshold and slo_burn firing/resolving transitions, per-host
    metric fan-out, `from_file` happy path + missing file + malformed `kind`),
    `test_scheduling_queue.py` (add `depth()` cases), `test_metrics_endpoint.py` (`/metrics` shape,
    auth-exempt, zero-traffic case), `test_alerts_endpoint.py` (`/v1/alerts` auth + content).
12. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
    .`, `uv run ruff format --check .`, `uv run mypy src`; then the `verify` skill against a real
    running LM Studio to confirm `/metrics` and `/v1/alerts` behave correctly end-to-end (not just
    against mocked backends), per the lesson learned during #11 (undocumented error-path status
    codes only surfaced by a real backend, not the mocked test suite).

Implemented test-first (`tdd` skill), in this order: `SchedulingQueue.depth()` (trivial, no
dependencies) → `MetricsRegistry` fully (pure, no FastAPI dependency) → `AlertEvaluator` fully
(pure, depends only on `SliSnapshot`) → app wiring (`/metrics`, `/v1/alerts`, the `/health/ready`
evaluate hook, the `chat_completions` recording hooks) → runbooks/docs last.

## Risks

- **Alert freshness depends on something calling `/health/ready`**: if no external prober or cron
  hits that endpoint, `AlertEvaluator` never re-evaluates and `GET /v1/alerts` reports stale
  state. This is the same operational assumption `expire_stale` already relies on (documented
  there, not solved here) — worth calling out to the operator in the runbook docs rather than
  silently assumed.
- ~~**Token usage isn't recorded for streaming responses**~~ — resolved by
  [ADR-0003](../adr/0003-lmstudio-backend-always-streams-internally.md): `LMStudioBackend` now
  requests `stream_options.include_usage` and `_stream_chunks` records token usage from the
  accumulated stream the same way the non-streaming path always has.
- **Existing-test churn is broad but shallow again**: same shape as the #11 `key_store` addition —
  every `create_app` call site needs one more constructor arg. Use the same shared-helper
  mitigation.
- **Hand-rolled Prometheus formatting**: no `prometheus_client` library means format correctness is
  entirely on this codebase's own tests — cover with an explicit test asserting the exact rendered
  text for a known snapshot (not just "response is 200"), so a future edit can't silently break
  the exposition format without a failing test.
- **Rolling-window eviction cost**: evicting on every `record_request` is O(evicted-count)
  amortized — fine at home-lab request rates; revisit if it ever shows up in profiling.
- **`slo_burn`'s single-window definition may be noisy**: a short burst can breach and resolve
  within one window without reflecting a real sustained problem. Accepted simplification per the
  spec; revisit with real operational data once this has run for a while.

## Open Questions

- Same as the spec's: whether multi-window SLO burn detection is eventually needed, and whether
  the rolling window should become configurable, are both deferred until real usage suggests the
  fixed single-window approach is insufficient.

## Addendum: a runbook per rule, not just critical ones

Step 7 originally scoped runbooks to the two `critical` default rules only. During
implementation, `AlertRule.runbook_url` turned out to be a required field regardless of severity
(every rule needs one), so a `warning`-severity rule with no matching doc would have shipped a
dangling link. Wrote a third short runbook (`docs/runbooks/p95-latency-threshold.md`) for
consistency rather than leaving that field pointing at a nonexistent file.
