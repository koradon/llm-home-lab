# Unbounded gap timeout by default for LM Studio and llama-server backends

## Status

accepted

## Context and Problem Statement

ADR-0003 raised `LMSTUDIO_TIMEOUT`'s default from 30s to 120s and made it a *gap* timeout (max
silence between chunks) rather than a cap on total generation time, but kept it a fixed number of
seconds and flagged its own limit: "if `LMSTUDIO_TIMEOUT=120` still proves too short for real
workloads (very large context windows or very slow hardware), consider making it effectively
unbounded for same-machine/trusted local backends ... rather than continuing to raise a fixed
number."

That trigger fired: routing a real curation workload (facts-service, a Tripper sub-project) with a
~250,000-character/~60k-token prompt through the orchestrator to a registered LAN host hit `504
backend_timeout` on nearly every call. The pattern (fail at ~120s, retry, succeed ~120-145s later)
matches the *first* chunk — i.e. the model's prefill on a large prompt — taking longer than 120s,
not a hung backend. `LLAMASERVER_TIMEOUT` has the identical default and semantics, so the same
failure mode applies there too.

## Considered Options

- **A — Raise the fixed default again** (e.g. 120s → 600s to match facts-service's own client-side
  LLM-call timeout). Simple, but ADR-0003 already rejected this pattern: any fixed number is
  eventually wrong for some prompt/model/hardware combination, and this project's own intended
  workloads (large-context local curation) make "eventually" arrive quickly.
- **B — Make the gap timeout unbounded by default**, keeping the env var as an explicit opt-in
  ceiling for operators who want one. Matches ADR-0003's own revisit guidance: every registered
  host is by definition a trusted local/LAN backend (that's the orchestrator's whole premise), and
  a hung one is already caught independently by `GET /health/ready` and the background health
  monitor — the gap timeout's only remaining job (per ADR-0003) is distinguishing "still producing
  tokens" from "hung," and a sufficiently large prompt's prefill is legitimately silent for
  minutes on ordinary consumer hardware.
- **C — Timeout scales with a declared context window / prompt size.** More precise, but adds a
  configuration dimension (per-host prompt-size-to-timeout mapping) for a problem `GET
  /health/ready` already solves more simply. Rejected as overengineering for what B gets for free.

## Decision Outcome

Chosen option: **B**. `LMSTUDIO_TIMEOUT` and `LLAMASERVER_TIMEOUT` now default to unbounded
(`httpx.Timeout(None, connect=connect_timeout)`) when unset, empty, `"0"`, or `"none"`
(case-insensitive) — parsed by a shared `_parse_gap_timeout()` helper in `main.py`. Setting either
to an explicit number still installs a hard gap ceiling, for operators who specifically want one
(e.g. failing fast in front of a network path they don't fully trust). `LMSTUDIO_CONNECT_TIMEOUT` /
`LLAMASERVER_CONNECT_TIMEOUT` are unaffected — a dead/unreachable host still fails in 10s, not
never.

### Consequences

- Good, because large-prompt curation workloads (this project's actual use case) no longer pay a
  guaranteed ~2-minute failed-attempt-then-retry tax on every call once the prompt exceeds a
  couple of minutes of prefill time.
- Good, because it removes a number that would otherwise need re-tuning per host (GPU vs. CPU,
  model size, context window) — `GET /health/ready` already covers "is this backend alive" without
  a per-workload guess.
- Bad, because a genuinely hung (not dead, not disconnected — stuck) backend that still holds its
  TCP connection open no longer self-resolves via `504` on the request path; recovery now depends
  entirely on the background health monitor marking the host unhealthy and routing around it.
  Accepted per ADR-0003's own framing: a connect-level problem is caught fast (10s); anything after
  that is "the model is thinking" until proven otherwise.
- Bad, because a concurrency slot (`*_MAX_CONCURRENT_REQUESTS`) can now be held for as long as a
  single generation legitimately runs, with no orchestrator-side ceiling forcing it to give up —
  already an accepted trade-off from ADR-0003 for the streaming path; this extends it to the
  non-streaming path's effective ceiling too, since ADR-0003 already unified them onto the same
  internal streaming transport.

### Addendum: `check_health()` needed its own bounded timeout

The "Good" and "Bad" points above assume `GET /health/ready` independently catches a hung backend
regardless of the gap timeout setting. That didn't hold in the initial implementation:
`check_health()` reused the same httpx client as `complete()`/`stream()`, so an unbounded gap
timeout left the health check itself unbounded too — a backend that accepted the TCP connection but
never answered `/health` (or `/v1/models` for LM Studio) hung `check_health()` forever, which in
turn blocked `_probe_all_hosts()`'s sequential per-host loop (`api/app.py`), stalling health
reporting for every registered host, not just the unresponsive one.

Fixed by giving `check_health()` its own `health_timeout` (default 10s, `LMSTUDIO_HEALTH_TIMEOUT` /
`LLAMASERVER_HEALTH_TIMEOUT`), passed as a per-request httpx timeout override independent of the
gap timeout — mirroring how `connect_timeout` already stays independent of it.

### Addendum: `_probe_all_hosts()` needed to probe hosts concurrently, not sequentially

Bounding `check_health()` still wasn't enough with more than one unreachable host: the sequential
`for host in registry.hosts(): await backend.check_health()` loop in `_probe_all_hosts()`
(`api/app.py`) paid the per-host timeout once per unreachable host, not once total. Two
simultaneously-dead hosts meant `/health/ready` — and the TUI dashboard's own health-check trigger,
which shares this same call path — took 2x `health_timeout` to respond; N dead hosts, Nx. A
dashboard polling every couple of seconds effectively stalled for as long as it took every dead
host to time out in turn.

Fixed by probing every host concurrently (`asyncio.gather` over one `_probe_one_host()` coroutine
per host) so N unreachable hosts still cost at most one `health_timeout`, not N of them.
