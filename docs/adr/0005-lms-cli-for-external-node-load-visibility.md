# Use the `lms` CLI as an optional external-load prober

## Status

accepted

## Context and Problem Statement

The orchestrator's own `in_flight` counter (`HostRegistry`) only reflects requests *it* dispatched.
It has no visibility into a registered LM Studio host being used directly by something else (a
developer testing locally, a different tool pointed straight at the same LM Studio instance) —
confirmed by checking LM Studio's REST API docs directly (both `/api/v0/*` and the newer
`/api/v1/*`, released in LM Studio 0.4.0): neither exposes a load/queue/active-generation
endpoint. The only place LM Studio reports this is `lms ps --json` (LM Studio's companion CLI,
`lms`), which as of LM Studio 0.3.27 reports each loaded model's `status` (e.g. `"idle"` /
`"processingPrompt"`) and `queued` (pending prediction count) — and, confirmed live against both a
local and a real remote host, `lms ps --host <ip> --json` connects over the *same* port already
used for the REST API (verified via `lsof` during a live call — all connections went to
`<ip>:1234`, the standard LM Studio server port), not a separate control port. `lms` itself
installs standalone (`curl -fsSL https://lmstudio.ai/cli/install.sh | bash`), independent of the
full desktop app.

This is the only way to see load caused by something other than this orchestrator. Every other
piece of this codebase talks to LM Studio purely over HTTP (httpx); this would be the first
subprocess/external-binary dependency.

## Considered Options

- **A — Don't build this.** Simplest, but leaves the operator's original question ("is something
  else hitting my LM Studio?") unanswerable short of manually checking LM Studio's own UI or OS
  task manager on each host.
- **B — Shell out to `lms ps --host <ip> --json` per registered host, as an optional, best-effort
  signal.** Requires a new subprocess dependency and the `lms` binary present wherever the
  orchestrator runs (including in its Docker image — one added install step, not the full desktop
  app, per investigation above). Must degrade gracefully (binary missing, host unreachable, LM
  Studio too old to support `--host`) rather than affect the orchestrator's core request path.
- **C — Ask the operator to run `lms ps` manually / check LM Studio's own UI when curious.**
  Zero engineering cost, but does nothing for the stated goal of seeing this in the TUI at a
  glance.

## Decision Outcome

Chosen option: **B**, scoped as a genuinely optional, degrading-gracefully signal — never a hard
dependency of the orchestrator's request-serving path. A new `ExternalLoadProbe` component runs
`lms ps --host <base_url's host> --json` per registered host (see
[external-node-load-visibility spec](../specs/20260720-external-node-load-visibility.md) for
exact behavior), on the same trigger as the existing health-check sweep, cached for a short
interval so `/health/ready` doesn't spawn a fresh subprocess on every call. `GET /v1/nodes` and the
TUI gain a field for it, always able to render "unavailable" without that ever being treated as an
error.

### Consequences

- Good, because it directly answers the operator's question (external load, not just
  orchestrator-originated) using a source LM Studio itself already provides, rather than inventing
  new instrumentation on the LM Studio side (which isn't possible — it's closed-source).
- Good, because it reuses the exact network path/port already open for the REST API — no new
  firewall/network configuration for operators who already have a node registered.
- Bad, because it's this project's first subprocess/external-binary dependency — a `lms`-missing
  environment (most likely: a from-scratch Docker image before the install step is added) must not
  degrade the orchestrator's core function, only this one optional signal.
- Bad, because `lms ps --host` is not part of LM Studio's documented, versioned REST API contract
  — it's a CLI feature that could change shape or behavior across LM Studio versions without the
  same stability guarantee as `/v1/chat/completions`. Treated as best-effort, not authoritative.
- Bad, because spawning a subprocess per registered host on every probe cycle is heavier than an
  HTTP call; mitigated by a cache interval (see spec) rather than probing on every single
  `/health/ready` call.
- Neutral: this does not change `_eligible_candidates`/routing at all — external load is a
  read-only, informational signal, not a scheduling input, at least for now.

- **Revisit trigger**: if LM Studio ever exposes this natively over its REST API (`/api/v1/*` is
  actively evolving per its changelog), drop the subprocess approach in favor of the HTTP
  equivalent — re-check the API changelog before extending this further.
