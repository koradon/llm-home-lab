# Per-model concurrency and context limits on registered nodes

## Status

accepted

## Context and Problem Statement

Multi-model translation workloads route different target languages to different models (for example `gemma-4-26B-A4B`, `Hy-MT2-30B-A3B`, and `EuroLLM-22B`) across nodes running `llama-server` in router mode with `--models-max 1`. Each node serves one model at a time, and each model has its own safe concurrency slot count and context window on that hardware.

Today `HostCapabilities` and `HostCapacity` only store node-level `max_concurrent_requests` and `context_window`. Flipping a node from one model to another via `PATCH /v1/nodes/{host_id}` therefore requires patching `allowed_models` and remembering to patch the node-level `max_concurrent_requests` and `context_window` alongside it. We want a single `PATCH` of `allowed_models` to switch both the served model and its effective limits, while remaining strictly additive and backward-compatible with all existing node configs, SQLite rows, API clients, and TUI flows.

## Considered Options

- **Option A — Single `model_limits` map on `HostCapabilities`**: add one optional field `model_limits: dict[str, ModelLimits] | None = None`, where each entry is an object with optional `max_concurrent_requests: int | None` (> 0) and `context_window: int | None` (> 0). Example:
  ```json
  {
    "allowed_models": ["gemma-4-26B-A4B"],
    "max_concurrent_requests": 4,
    "context_window": 8192,
    "model_limits": {
      "gemma-4-26B-A4B": {"max_concurrent_requests": 8, "context_window": 16384},
      "Hy-MT2-30B-A3B": {"max_concurrent_requests": 6}
    }
  }
  ```
- **Option B — Separate parallel maps**: add `model_max_concurrent_requests: dict[str, int] | None` and `model_context_windows: dict[str, int] | None`, mirroring `model_sizes_gb` and `model_aliases`.
- **Option C — Full router model catalog (`models` + `swap_mode`)**: introduce a full Phase-1 router catalog with runtime model state and per-`(node, model)` health now.

## Decision Outcome

Chosen option: **Option A (`model_limits`)**, because it is the smallest schema that keeps a model's concurrency and context overrides together under the exact existing field names (`max_concurrent_requests`, `context_window`) without adding two parallel maps to keep in sync or pulling full Phase-1 router state into this change.

Resolution rules:

1. **Per-request and model-filtered resolution (`model` is known)**: look up `model` in `host.capabilities.model_limits`. Use the entry's `max_concurrent_requests` (or `context_window`) when set; otherwise fall back to the node-level `host.capacity.max_concurrent_requests` (or `host.capabilities.context_window`). Dispatch gating (`_eligible_candidates`, `SchedulingQueue`), load-ratio scoring, routing context-window checks, and `GET /v1/capacity?model=<name>` all use this per-model resolution.
2. **Node-level effective resolution (`model` is not specified)**: when `allowed_models` contains exactly one model, resolve against that single model's `model_limits` entry (falling back to the node-level value for any omitted field). When `allowed_models` is `None`, empty, or lists multiple models, fall back to the node-level value.
3. **Observability and backward compatibility**:
   - `GET /v1/nodes` retains `max_concurrent_requests` and `context_window` as the node's configured base/fallback values (so existing edit forms and clients never clobber fallbacks) and adds `model_limits`, `effective_max_concurrent_requests`, and `effective_context_window`.
   - For TUI capacity display, see the [dashboard specification](../specs/20260719-tui-operator-dashboard.md#behavior).
   - Unscoped `GET /v1/capacity` sums configured node-level `max_concurrent_requests` over online hosts.
   - `HostRegistryStore` defaults missing `model_limits` in legacy SQLite JSON rows to `None`.

### Consequences

- Good, because flipping a single-model router node requires only `PATCH /v1/nodes/{host_id} {"allowed_models": ["<next-model>"]}` — dispatch gating, model-filtered `/v1/capacity`, `/v1/nodes`, and the TUI immediately reflect the new model's slot limit and context window.
- Good, because existing configs, SQLite databases, and clients that omit `model_limits` behave identically to before.
- Good, because `model_limits` is a single optional field that can be extended later if needed without breaking callers.
- Bad, because older orchestrator binaries cannot deserialize SQLite rows that contain the new `model_limits` key if rolled back without migrating the stored JSON (the same forward-only caveat noted for earlier capability fields like `position`).

## Related

- Issue: https://github.com/koradon/llm-home-lab/issues/86
- ADR: [0008-dedicated-capacity-summary-endpoint](0008-dedicated-capacity-summary-endpoint.md)
- ADR: [0012-forward-generation-parameters-to-backends](0012-forward-generation-parameters-to-backends.md)
- Modules: `src/llm_home_lab/registry/models.py`, `src/llm_home_lab/api/app.py`, `src/llm_home_lab/scheduling/queue.py`, `src/llm_home_lab/tui/app.py`
