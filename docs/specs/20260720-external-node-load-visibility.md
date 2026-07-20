# External Node Load Visibility

## Status

draft

## Summary

An optional, best-effort `ExternalLoadProbe` that shells out to LM Studio's companion CLI (`lms
ps --host <host> --json`) per registered node, so the operator can see load on a host caused by
something other than this orchestrator — a use case the orchestrator's own `in_flight` counter
cannot cover, since it only reflects requests the orchestrator itself dispatched. Per
[ADR-0005](../adr/0005-lms-cli-for-external-node-load-visibility.md), this is this project's first
subprocess/external-binary dependency, and it must never affect the orchestrator's core
request-serving path if the `lms` binary is missing, times out, or the target host doesn't support
`--host`.

## User stories

- As an operator, I want to see whether a registered LM Studio host is busy with generations I
  didn't initiate, so that I know whether another tool is hitting it directly.
- As an operator without `lms` installed (or on a host that doesn't support remote `lms` queries),
  I want the orchestrator and TUI to keep working normally, with this signal simply showing as
  unavailable, so that a missing optional tool never breaks anything else.
- As an operator, I want this check to not slow down `/health/ready` on every single call, so that
  a frequent external prober isn't spawning a new subprocess per host every time it polls.

## Requirements

- New module `src/llm_home_lab/registry/external_load.py`:
  - `ExternalLoadStatus` (dataclass): `available: bool`, `status: str | None` (LM Studio's own
    per-model `status` string, e.g. `"idle"`/`"processingPrompt"`, taken from whichever loaded
    model reports the highest activity if more than one is loaded), `queued: int | None` (summed
    across loaded models), `checked_at: datetime`.
  - `ExternalLoadProbe(lms_binary: str = "lms", timeout_s: float = 5.0, cache_ttl: timedelta =
    timedelta(seconds=15))`:
    - `async def probe(host_id: str, base_url: str, at: datetime) -> ExternalLoadStatus` — if a
      cached result for `host_id` is younger than `cache_ttl` relative to `at`, returns it
      unchanged (no subprocess spawned). Otherwise runs
      `lms ps --host <hostname-from-base_url> --json` via `asyncio.create_subprocess_exec` (never
      `shell=True`), enforcing `timeout_s` via `asyncio.wait_for`.
    - Extracts the hostname (not scheme/port) from `base_url` for the `--host` argument, since
      `lms`'s own default port matches LM Studio's server port already in `base_url`.
    - On success: parses stdout as the JSON array `lms ps --json` documents (one entry per loaded
      model); `queued` is `sum(entry["queued"] for entry in entries)`; `status` is `"idle"` only if
      every entry is `"idle"`, otherwise the first non-idle entry's status. Zero loaded models is
      `available=True, status="idle", queued=0` (a host with nothing loaded has no external load).
    - On any failure — `lms` binary not found, non-zero exit, timeout, unparseable JSON/stdout —
      returns `ExternalLoadStatus(available=False, status=None, queued=None, checked_at=at)`,
      logged once at `INFO` (not `ERROR`; a missing optional tool is not an orchestrator problem).
- `main.py` gains `ORCHESTRATOR_LMS_BINARY_PATH` (default `"lms"`, resolved via `PATH`) and
  `ORCHESTRATOR_EXTERNAL_LOAD_PROBE_INTERVAL_S` (default `15`), wired into `ExternalLoadProbe`.
- `GET /v1/nodes` gains an `external_load` field per host:
  `{"available": bool, "status": str | null, "queued": int | null}`, sourced from
  `ExternalLoadProbe.probe(...)`, called from the same place `/health/ready`'s per-host loop
  already lives (see [multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md)),
  not from `GET /v1/nodes` itself (which stays a fast, no-I/O read of already-known state, matching
  its current behavior for `status`/`_node_status`).
- TUI (`src/llm_home_lab/tui/app.py`) Nodes panel gains a column for this — rendered similarly to
  the existing node `status` column (colored), e.g. `unavailable` in a muted style, `idle` neutral,
  anything else (busy) highlighted, with `queued` shown alongside when `> 0`.

## Behavior

**This is read-only and never affects routing.** `_eligible_candidates`/scheduling behavior is
unchanged; external load is informational only.

**A missing `lms` binary degrades this one signal, not the orchestrator.** `FileNotFoundError` from
`asyncio.create_subprocess_exec` is caught the same way any other probe failure is — the field
reads `available: false` everywhere it's surfaced; nothing else fails or logs above `INFO`.

**Caching avoids a subprocess storm.** If `/health/ready` (or whatever else triggers a probe) is
called more often than `cache_ttl`, only the first call in that window actually spawns `lms`; the
rest reuse the cached `ExternalLoadStatus`.

**A host with nothing loaded reports idle, not unavailable.** An empty `lms ps --json` array (no
models currently loaded on that host) is a successful, meaningful result — `available=True`,
`queued=0` — distinct from a failed probe.

**Per-host failures are independent.** One host's `lms` probe failing (e.g., its LM Studio version
predates `--host` support, or the machine is off) does not affect any other host's probe.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file:
`docs/specs/features/20260720-external-node-load-visibility.feature`.

## Related

- ADR: [0005-lms-cli-for-external-node-load-visibility](../adr/0005-lms-cli-for-external-node-load-visibility.md)
- Spec: [multi-node-registry-and-scheduler](20260717-multi-node-registry-and-scheduler.md) —
  `GET /v1/nodes`, `_node_status`, and the `/health/ready` per-host loop this probe hooks into.
- Spec: [tui-operator-dashboard](20260719-tui-operator-dashboard.md) — Nodes panel this extends
  with a new column.
- Idea: [operator-observability-dashboards](../ideas/operator-observability-dashboards.md)
- Plan: (to be written) `docs/plans/20260720-external-node-load-visibility.md`
- Issue: (to be created, unattached to a milestone) `.plan/issues/`
- Acceptance: `docs/specs/features/20260720-external-node-load-visibility.feature`

## Open Questions

- Exact TUI styling for "busy due to external load" vs. the existing node `status` column's
  online/offline styling — left to the plan.
- Whether `cache_ttl`/probe interval should be configurable per-host (a slow-to-probe host vs. a
  fast one) — deferred; one global interval is the simpler starting point.
- Whether a future LM Studio REST API release makes this subprocess approach obsolete — flagged in
  ADR-0005's revisit trigger, not resolved here.
