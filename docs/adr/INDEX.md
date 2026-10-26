# ADR Index

| File | Title | Status |
| --- | --- | --- |
| `0001-python-as-implementation-language.md` | Use Python as the implementation language for the orchestrator | accepted |
| `0002-sqlite-for-session-storage.md` | Use SQLite for session state persistence | accepted |
| `0003-lmstudio-backend-always-streams-internally.md` | LM Studio backend always streams internally; raise default timeouts | accepted |
| `0004-persist-node-registry-no-auto-deregistration.md` | Persist the node registry; stop auto-deregistering unreachable hosts | accepted |
| `0005-lms-cli-for-external-node-load-visibility.md` | Use the `lms` CLI as an optional external-load prober | accepted |
| `0006-background-health-poller.md` | Background health poller, independent of any client | accepted |
| `0007-llamaserver-backend-shape.md` | Prefer a new backend's native capabilities over replicating another backend's workarounds | accepted |
| `0008-dedicated-capacity-summary-endpoint.md` | Expose a dedicated `GET /v1/capacity` endpoint instead of client-side aggregation | accepted |
| `0009-per-node-throughput-and-token-metrics-design.md` | Cumulative per-host counters, healthy-only inclusion, backend-call latency, and Nodes-table placement for per-node metrics | accepted |
| `0010-unbounded-gap-timeout-by-default-for-lmstudio-and-llamaserver.md` | Unbounded gap timeout by default for LM Studio and llama-server backends | accepted |
| `0011-in-memory-completion-log-for-windowed-throughput.md` | In-memory completion log for windowed tasks-per-hour | accepted |
| `0012-forward-generation-parameters-to-backends.md` | Forward generation parameters to backends | accepted |
| `0013-node-test-lease.md` | Lease a node with a heartbeat and a hard ceiling | accepted |
