# Per-Alias Completion Metrics

## Status

draft

## Summary

`model_aliases` lets one registered node round-robin a single client-facing model name across
several real LM Studio model identifiers loaded on that node (`LMStudioBackend._resolve_model()`,
`src/llm_home_lab/backends/lmstudio.py:41-50`) — for example, two loaded instances of the same
model, each with its own context window, sharing one name so callers don't have to pick an
instance themselves. Today, completion metrics (`MetricsRegistry`,
`src/llm_home_lab/observability/metrics.py`) are recorded and exposed keyed only by `host_id`. The
alias `_resolve_model()` actually picked for a given request is used to build the outbound LM
Studio payload and then discarded — it is never reported back to metrics, so there is no way,
even in principle, to see whether load is landing evenly across a host's aliased instances. This
spec defines the behavior for closing that gap. It does not cover TUI rendering of the resulting
data (tracked separately, see Related) and is not scheduled for immediate implementation.

## User stories

- As an operator running multiple instances of the same model on one node via `model_aliases`, I
  want to see completion counts/tokens broken down per resolved alias, so that I can tell whether
  the round-robin is actually distributing load evenly across instances.
- As an operator diagnosing a slow or stuck instance, I want per-alias throughput, so that I can
  tell one specific loaded instance apart from its siblings rather than only seeing the host as a
  whole.
- As an operator on a node with no `model_aliases` configured, I want metrics to look exactly as
  they do today, so that this change adds a capability without adding noise for the common case.

## Requirements

- The alias resolved by `LMStudioBackend._resolve_model()` for a given request must be threaded
  back out of the backend alongside the existing response/chunk data (e.g. a new field on
  `BackendResponse`/`BackendChunk` in `src/llm_home_lab/backends/base.py`), so that whatever calls
  `record_completion` today can also learn which alias served the request.
- A backend with no `model_aliases` entry for the requested model must report no alias (the
  resolved value equals the original model name — see
  `test_no_model_aliases_forwards_the_original_model_unchanged`, `tests/test_lmstudio_backend.py`),
  and metrics for that request must behave exactly as they do today: no new labels, no new series.
- `MetricsRegistry` gains an alias-keyed counter (`host_id` + resolved alias) alongside the
  existing host-keyed `_token_usage_total`, populated only when a request actually went through a
  configured alias.
- The Prometheus text exposition (`MetricsRegistry` rendering in
  `src/llm_home_lab/observability/metrics.py`) gains a new metric line per `(host_id, alias)` pair
  that has recorded at least one completion, following the existing
  `llm_home_lab_token_usage_total{host_id="..."}` pattern with an added `alias` label — not a
  replacement for the host-level metric, which must keep aggregating across all aliases of a host.
- `src/llm_home_lab/diagnostics/metrics_parser.py` gains parsing for the new metric line(s), making
  per-alias totals available to any consumer (TUI or otherwise) the same way `token_usage_total` is
  today.

## Behavior

**Backward compatible by default.** A node with no `model_aliases` configured, or a request for a
model absent from that node's `model_aliases` map, produces identical metrics output to today —
no alias label appears at all for that request.

**Per-alias data is additive, not a replacement.** The existing host-level
`token_usage_total{host_id="..."}` metric keeps aggregating every request to that host regardless
of which alias (if any) served it. The new alias-keyed metric is a finer-grained breakdown
available alongside it, not instead of it.

**Alias identity is the resolved LM Studio identifier, not the client-facing name.** For a node
configured with `model_aliases: {"my-model": ["my-model-a", "my-model-b"]}`, a request for
`my-model` that round-robins to `my-model-b` is recorded under alias `my-model-b`, not
`my-model` — the whole point is distinguishing the instances the client-facing name hides.

**Composes with the (separately planned) TUI "max" column.** If the TUI's Queue & Tokens panel
later grows a per-row historical maximum (see the node-registration-parity plan referenced below),
an alias-level row should get the same treatment — this spec only needs to ensure the underlying
data is available; the max-tracking behavior itself lives in the TUI plan, not here.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/20260730-per-alias-completion-metrics.feature`.

## Related

- Spec: [lmstudio-backend-adapter](20260711-lmstudio-backend-adapter.md) — `LMStudioBackend` and
  `_resolve_model()` this extends.
- Spec: [health-and-telemetry-baseline](20260711-health-and-telemetry-baseline.md) —
  `MetricsRegistry` and the Prometheus exposition format this adds a metric line to.
- Spec: [tui-operator-dashboard](20260719-tui-operator-dashboard.md) — Queue & Tokens panel that
  would eventually render this data.
- Plan: (to be written, not scheduled) `docs/plans/20260730-per-alias-completion-metrics.md`
- Out of scope: TUI rendering of per-alias data, and the unrelated node-registration-parity TUI
  work (register/edit modals, `model_aliases` editing UI, Queue & Tokens "max" column) — that work
  proceeds independently of this spec.

## Open Questions

- Whether the alias-keyed counter should live in `MetricsRegistry` itself or a separate registry —
  deferred to the implementation plan.
- Whether per-alias data should also flow into `SliSnapshot`/alerting, or stay diagnostics-only —
  no current use case demands it, so default to diagnostics-only until one appears.
- Retention/cardinality: a node with many short-lived or frequently reconfigured aliases could
  accumulate stale series over time — needs a decision (e.g. reset on `model_aliases`
  reconfiguration) before implementation.
