# Router-mode awareness and per-(node, model) health for llama-server nodes

## Status

accepted

## Context and Problem Statement

`llama-server` supports a multi-model router mode (`--models-preset ... --models-max 1`) in which one `llama-server` process manages a preset catalog of models and loads or evicts child model processes on demand. Our Mac mini nodes run in this mode with `--models-max 1`, serving one active model at a time and switching models via `PATCH /v1/nodes/{host_id}` of `allowed_models` (paired with per-model `model_limits` from [ADR-0015](0015-per-model-node-limits.md)).

Previously, the orchestrator assumed every `llamaserver` host was a single-model process ([ADR-0007](0007-llamaserver-backend-shape.md)). Against a router-mode `llama-server`, that assumption breaks in four concrete ways:

1. **Lost slot telemetry**: `GET /slots` without `?model=` returns HTTP 400 on a router node, leaving `external_load.available=false` in `GET /v1/nodes` and hiding slot counts and `tasks/h/slot`.
2. **Whole-node knock-out on one broken model**: completion failures are recorded only per node in `HealthMonitor`. Three failed requests against one broken preset mark the entire router node `offline` for the full cooldown (~30 s), blocking healthy models on the same node.
3. **Misleading model listing and missing visibility**: `GET /v1/models` on a router returns every preset and cache entry (including unloaded and broken models), and neither `GET /v1/nodes` nor the TUI exposes per-model router states (`loaded`, `loading`, `sleeping`, `unloaded`, `failed`).
4. **Generic errors while loading**: requests arriving while a model is loading fail with generic `503` or `400` responses rather than a distinct retryable loading signal.

We want Phase-1 router awareness to close these gaps while remaining strictly additive and backward-compatible so non-router nodes (`lmstudio` and single-model `llamaserver`) behave identically to before. Automatic orchestrator-driven model swapping (Phase 2), flip controllers, and per-node backend API keys remain out of scope.

## Considered Options

- **Option A — Minimal `swap_mode` field (`"none" | "router"`) on `HostCapabilities`, paired with live router probing and per-`(node, model)` health**:
  - Add one optional field `swap_mode: Literal["none", "router"] = "none"` to `HostCapabilities` (default `"none"`), accepted by `POST /v1/nodes/register` and `PATCH /v1/nodes/{host_id}`, persisted in SQLite with default `"none"` for legacy rows, and exposed by `GET /v1/nodes`.
  - Reuse existing `model_limits` ([ADR-0015](0015-per-model-node-limits.md)) for per-model concurrency/context overrides rather than duplicating a separate static catalog schema.
  - Probe `GET /models` for live model states and `GET /slots?model=<name>` for loaded model(s) on router nodes; track completion/load health per `(node, model)` inside `HealthMonitor`; surface model states in `GET /v1/nodes` and a TUI `models` column; return `503 model_loading` with `Retry-After` when a requested model is currently loading.
- **Option B — Separate `backend_type: "llamaserver_router"`**:
  - Register router nodes under a new backend type instead of a capability flag on `llamaserver`.
  - Rejected because a router node is still a `llamaserver` speaking the same `/v1/chat/completions`, `/health`, and `/slots` wire contract, and adding a new `backend_type` would fork factory registration, load-probe dispatch, and operator TUI backend selection for a mode difference that belongs on capabilities.
- **Option C — Full Phase-2 router catalog (`models` map with footprints, load timeouts, and swap state machine) now**:
  - Rejected as premature: Phase 2 automatic swapping is out of scope, and `model_limits` already covers per-model slot and context limits cleanly.

## Decision Outcome

Chosen option: **Option A (`swap_mode: "none" | "router"` with live `/models` + `/slots?model=` probing and per-`(node, model)` health)**, because it is the smallest schema extension that fits `HostCapabilities` alongside `model_limits` while keeping non-router nodes completely unchanged.

Key contracts:

1. **Registry schema**:
   - `HostCapabilities.swap_mode` defaults to `"none"`; `"router"` enables router awareness.
   - `HostRegistryStore` defaults missing `swap_mode` in legacy SQLite JSON rows to `"none"`.
   - `GET /v1/nodes` includes `"swap_mode"` on every node and `"models"` (`null` for non-router nodes; a list of `{"id", "state", "healthy", "recent_failures"}` entries for router nodes).
2. **Router probing and `list_models`**:
   - On `swap_mode == "router"` nodes, host probes read `GET /health` for node liveness, `GET /models` for the router model catalog and states (`loaded`, `loading`, `sleeping`, `unloaded`, `failed`), and `GET /slots?model=<name>` for currently loaded model(s) (falling back to `sleeping` if none are `loaded`; reporting `external_load.available=false` without calling parameterless `GET /slots` when no model is loaded).
   - `LlamaCPPServerBackend.list_models()` in router mode queries `GET /models` and returns only models currently servable (`loaded` or `sleeping`, never `unloaded`, `loading`, or `failed`). In `swap_mode == "none"`, it calls `GET /v1/models` unchanged.
3. **Per-`(node, model)` health**:
   - `HealthMonitor` tracks per-`(backend_id, model)` health using the same `failure_threshold`, `recovery_threshold`, and `cooldown` rules as node-level health.
   - On router nodes, transport connection failures (`BackendConnectionError`) and `GET /health` failures remain node-level; model/completion failures (`BackendResponseError`, `BackendTimeoutError`, degenerate completions) and router-reported `failed: true` model entries isolate to `(node, model)`.
   - An unhealthy or router-failed `(node, model)` pair is excluded from routing and contributes `0` to `GET /v1/capacity?model=<name>`, while the node stays `online` and other models on the same node continue serving and reporting capacity.
   - Completion-failure recovery is half-open: after cooldown exactly one trial request may be admitted for that `(node, model)`; success closes the circuit, failure restarts cooldown, and capacity stays `0` until closed. Router-reported `failed` entries still block admission until `GET /models` clears `failed`.
4. **`503 model_loading` with retry hint**:
   - When a request arrives for a model that is currently `loading` on every eligible router host (or on a leased router host, or if a router responds with a loading `503`), the gateway returns HTTP `503` with `error.code = "model_loading"`, `error.type = "backend_error"`, and a `Retry-After` header (default `5` seconds), without counting a node or model failure.
5. **TUI visibility**:
   - The Nodes table adds a trailing `models` column showing color-coded per-model states for router nodes (`—` for non-router nodes).

### Consequences

- Good, because router nodes regain accurate `external_load` (`total_slots`, `busy_slots`) and `tasks/h/slot` telemetry without sending failing parameterless `GET /slots` requests.
- Good, because one broken preset no longer knocks an entire router node offline or zeros capacity for healthy models on that node.
- Good, because callers can distinguish a transient `503 model_loading` (with `Retry-After`) from hard failures and retry cleanly.
- Good, because `swap_mode="none"` (the default) preserves exact existing behavior for LM Studio and single-model `llama-server` nodes.
- Bad, because older orchestrator binaries cannot deserialize SQLite rows containing the new `swap_mode` key if rolled back without removing that key from stored JSON (the same forward-only caveat noted in [ADR-0015](0015-per-model-node-limits.md)).

## Related

- Issue: https://github.com/koradon/llm-home-lab/issues/88
- ADR: [0007-llamaserver-backend-shape](0007-llamaserver-backend-shape.md) — single-model baseline extended for router mode by this ADR
- ADR: [0015-per-model-node-limits](0015-per-model-node-limits.md) — per-model `max_concurrent_requests` and `context_window` overrides that pair with `swap_mode: "router"`
- Spec: [llamaserver-backend-adapter](../specs/20260804-llamaserver-backend-adapter.md)
- Spec: [tui-operator-dashboard](../specs/20260719-tui-operator-dashboard.md)
- Modules: `src/llm_home_lab/registry/models.py`, `src/llm_home_lab/registry/llamaserver_load.py`, `src/llm_home_lab/registry/multi_backend_load_probe.py`, `src/llm_home_lab/backends/llamaserver.py`, `src/llm_home_lab/health/monitor.py`, `src/llm_home_lab/api/app.py`, `src/llm_home_lab/tui/app.py`
