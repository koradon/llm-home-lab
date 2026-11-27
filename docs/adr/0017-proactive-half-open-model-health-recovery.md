# Proactive half-open per-(node, model) health recovery without traffic

## Status

accepted

## Context and Problem Statement

[ADR-0016](0016-llamaserver-router-mode-awareness.md) added per-`(node, model)` health tracking for `llama-server` router-mode nodes (`swap_mode: "router"`). When a `(node, model)` pair crosses `failure_threshold`, `HealthMonitor` marks only that pair unhealthy while keeping the router node `online` and its other models serving.

Previously, recovery of an unhealthy `(node, model)` pair was strictly traffic-driven: after the cooldown (default 30 s), `may_admit_model` and `claim_model_trial` admitted a single incoming client completion request as a half-open trial, and only that request's outcome closed or reopened the circuit.

In practice, an idle unhealthy pair receives no client requests — for example, after a model flip on a Mac mini `--models-max 1` router node, external controllers (such as facts-service's flip controller) wait for `GET /v1/nodes` to report the loaded model as `healthy: true` (or `GET /v1/capacity?model=<name>` to report positive capacity) before routing traffic to it. Because nothing routes a request while capacity is zero and the pair is reported unhealthy, the model stays stuck as `loaded` + `healthy: false` indefinitely unless the client works around it by injecting synthetic canary requests. This mirrors the node-level liveness gap fixed in [ADR-0006](0006-background-health-poller.md): health state must recover on its own without depending on external clients driving probes or traffic.

## Considered Options

- **Option A — Proactive half-open single-token trial probe during periodic host probing (strictly gated to `loaded`, allowed, unleased router models)**:
  - During `_probe_one_host` (driven by the existing background health poller and `/health/ready`), after `GET /health` succeeds and `GET /models` is read on a `swap_mode: "router"` node, check each model that the router reports as `state == "loaded"` (and `failed == False`).
  - If the host is healthy, not leased, has a free concurrency slot, the model is permitted by `allowed_models` (when set), and `HealthMonitor.claim_unhealthy_model_trial` succeeds (pair is unhealthy, cooldown has elapsed, and `trial_in_flight` is `False`), send a single minimal completion (`max_tokens=1`, bounded by `health_timeout`).
  - Success closes the circuit via `record_model_probe(..., healthy=True)`; failure (backend error, timeout, unexpected exception, or degenerate empty output) restarts the cooldown via `record_model_probe(..., healthy=False)`; a `503` loading response releases the trial without counting a failure.
  - Shares `trial_in_flight` with request-driven trials (`claim_model_trial`), so a probe and a client request can never run concurrent trials against the same `(node, model)` pair.
  - Non-router nodes (`swap_mode == "none"`) and healthy pairs send zero extra requests.
- **Option B — Clear the failure counter / mark the pair healthy whenever `GET /models` reports `state == "loaded"` and `failed == False`**:
  - Rejected because `GET /models` only reflects router process/catalog state, not inference health. A model can remain `loaded` in `llama-server` while its inference path is broken (returning HTTP 500s, timing out, or emitting degenerate empty outputs). Clearing failures on a `/models` read alone would silently hide real model failures and cause repeated flapping every poll tick.
- **Option C — Probe every unhealthy tracked model regardless of router state (`unloaded`, `loading`, `sleeping`) or `allowed_models`**:
  - Rejected because sending `/v1/chat/completions` for an `unloaded` or `sleeping` model on a `--models-max 1` router triggers an unwanted model load and evicts the currently active model, and probing disallowed models violates the operator's `allowed_models` boundary.
- **Option D — Keep recovery strictly traffic-driven and require clients/controllers to send canary requests**:
  - Rejected because every client or flip controller would have to duplicate canary logic to discover whether an idle node recovered, violating the orchestrator's ownership of health state established in [ADR-0006](0006-background-health-poller.md).

## Decision Outcome

Chosen option: **Option A (proactive half-open single-token trial probe during periodic host probing, gated to `loaded`, allowed, unleased router models)**, because it verifies real inference recovery without waiting for client traffic, cannot trigger model loads or evictions, respects leases and `allowed_models`, never races with client-driven trials, and adds negligible cost (at most one 1-token completion per unhealthy loaded pair per cooldown window).

Key contracts:

1. **Shared half-open trial coordination**:
   - `HealthMonitor.claim_unhealthy_model_trial(backend_id, model, at)` atomically checks that `(backend_id, model)` is currently unhealthy, cooldown has elapsed, and `trial_in_flight` is `False`, then sets `trial_in_flight = True`.
   - `claim_model_trial` delegates to `claim_unhealthy_model_trial` for unhealthy pairs, so periodic probes and incoming client requests share the exact same single-trial gate.
2. **Strict probe guards**:
   - Only runs on `swap_mode == "router"` nodes when node-level health is healthy.
   - Only probes models reported by `GET /models` as `state == "loaded"` and `failed == False` — never `unloaded`, `loading`, `sleeping`, or `failed`.
   - Never probes a model excluded by `allowed_models` (when `allowed_models` is not `None`).
   - Never probes a leased node (`lease_book.is_held`), and never settles a probe if a lease appears mid-flight.
   - Skips probing if the host is currently at its effective concurrency limit, and holds a registry concurrency slot while the probe is in flight (released by the `registration_id` that was acquired).
   - Bail without settling if the live `HostInfo` object identity changes mid-probe (soft re-register or DELETE+re-register), matching the chat-completions reservation check.
3. **Minimal probe payload and outcome classification**:
   - Sends a minimal 1-token completion (`max_tokens=1`), bounded by the backend's `health_timeout` (default 10 s), without recording synthetic probes in user throughput (`completion_log`) or completion metrics.
   - Because `max_tokens=1` naturally stops at the token limit, probe responses accept `finish_reason == "stop"` with non-empty content as well as `finish_reason == "length"` when content is non-empty or `completion_tokens > 0` (covering reasoning models whose first token may be emitted as reasoning).
   - Success closes the circuit; backend errors, timeouts, unexpected exceptions, or degenerate outputs record a model failure and restart the cooldown; `503` loading responses release `trial_in_flight` without counting a failure.
4. **Non-router nodes unchanged**:
   - Nodes with `swap_mode == "none"` never execute router model recovery probing.

### Consequences

- Good, because idle unhealthy `(node, model)` pairs on router nodes recover automatically after cooldown without client traffic or external canary hacks, restoring `GET /v1/nodes` (`healthy: true`) and `GET /v1/capacity?model=`.
- Good, because probing strictly `loaded`, non-failed, allowed models on unleased nodes guarantees zero unintended model loads, zero evictions on `--models-max 1` hosts, and zero interference with leased test nodes.
- Good, because real inference is verified before closing the circuit — broken models that still appear `loaded` in `GET /models` stay unhealthy instead of flapping back into routing.
- Good, because probe overhead is bounded to at most one 1-token completion per unhealthy loaded pair per cooldown (30 s default), and zero when all models are healthy.
- Bad, because an unhealthy model that stays permanently `loaded` and permanently broken receives one 1-token probe per cooldown window until unloaded, removed from `allowed_models`, or fixed.

## Related

- Issue: https://github.com/koradon/llm-home-lab/issues/90
- ADR: [0006-background-health-poller](0006-background-health-poller.md) — node-level precedent for orchestrator-owned recovery independent of clients
- ADR: [0016-llamaserver-router-mode-awareness](0016-llamaserver-router-mode-awareness.md) — router-mode awareness and per-`(node, model)` half-open health extended by this ADR
- Spec: [llamaserver-backend-adapter](../specs/20260804-llamaserver-backend-adapter.md)
- Spec: [failover-and-health-policy](../specs/20260717-failover-and-health-policy.md)
- Modules: `src/llm_home_lab/health/monitor.py`, `src/llm_home_lab/api/app.py`
