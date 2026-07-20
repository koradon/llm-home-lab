# llm-home-lab

A local LLM orchestrator: one OpenAI-compatible API endpoint (`/v1/chat/completions`) that
routes requests across one or more local model backends (LM Studio, with more backends possible
later). Point your agent/tool at the orchestrator instead of directly at a model server, and it
handles routing, failover, capacity limits, and auth for you.

```text
Agent (OpenCode / other)  --Bearer key-->  Orchestrator  -->  LM Studio (one or more hosts)
```

## Quickstart

### 1. Set up LM Studio (the model backend)

1. Open LM Studio, load a model.
2. Go to the **Local Server** tab (the `<->` icon in the left sidebar) and click **Start Server**.
3. Note the port shown — it defaults to `1234`. LM Studio now serves an OpenAI-compatible API at
   `http://localhost:1234/v1`.

### 2. Create an API key for the orchestrator

The orchestrator requires a Bearer token on every request except the health endpoints. Create
`config/api_keys.json`:

```json
{
  "clients": [
    {
      "client_id": "my-agent",
      "allowed_path_prefixes": ["/v1/chat/completions"],
      "keys": [{"key": "sk-dev-changeme", "expires_at": null}]
    }
  ]
}
```

Pick your own key value (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
See [docs/security-baseline.md](docs/security-baseline.md) for the full format, how to add more
clients, and how to rotate a key later.

**Just testing locally and don't want to deal with keys yet?** Set `ORCHESTRATOR_AUTH_ENABLED=false`
and skip this step entirely — every request is admitted with no key required. Don't do this
outside your own machine.

### 3. Start the orchestrator

Both of these are equally supported — pick whichever fits your setup.

**Option A — uv (runs natively on your machine):**

```bash
uv sync
LMSTUDIO_BASE_URL=http://localhost:1234 uv run llm-home-lab
```

**Option B — Docker:**

```bash
docker compose up --build
```

`docker-compose.yml` points `LMSTUDIO_BASE_URL` at `host.docker.internal` by default, since LM
Studio runs on your host machine, not inside the container. Adjust it if LM Studio runs
elsewhere on your network.

Either way, the orchestrator listens on `:8080` (override with `ORCHESTRATOR_PORT`).

### 4. Send a request

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-changeme" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any-model-name",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

If you get `401`, check your `Authorization` header and that `config/api_keys.json` exists and
matches `ORCHESTRATOR_API_KEYS_FILE` (see below). If you get `503`, LM Studio's server isn't
reachable at `LMSTUDIO_BASE_URL` — check step 1. If you get `400 model_not_available`, the model
you named isn't currently loaded in LM Studio — see the note on `allowed_models` below for why the
orchestrator won't just load it for you.

## Adding more model hosts

Once the orchestrator is running, register additional LM Studio instances (other machines on
your network) without restarting it:

```bash
curl -X POST http://localhost:8080/v1/nodes/register \
  -H "Authorization: Bearer <a key allowed on /v1/nodes>" \
  -H "Content-Type: application/json" \
  -d '{
    "host_id": "gpu-box",
    "backend_type": "lmstudio",
    "base_url": "http://gpu-box.home:1234",
    "context_window": 8192,
    "max_concurrent_requests": 4
  }'
```

The orchestrator then routes and load-balances across every registered, healthy host. See
`GET /v1/nodes` to list registered hosts, `POST /v1/nodes/{host_id}/heartbeat` to keep one alive,
and `DELETE /v1/nodes/{host_id}` to remove one.

**Avoiding surprise model loads**: LM Studio will just-in-time load any model in its catalog the
moment it's requested — even one you never intended to run, which can silently eat all your RAM
if a client sends an unexpected `model` value. Without an explicit `allowed_models` list, the
orchestrator asks LM Studio which models are *currently loaded* and only routes to those,
rejecting anything else with `400`. Pin it down further (or skip that extra check per-request)
by registering with a fixed list:

```json
{ "host_id": "gpu-box", "...": "...", "allowed_models": ["qwen2.5-coder-14b-instruct-mlx"] }
```

If you actually *want* on-demand loading (rather than requiring everything pre-loaded), declare a
memory budget instead — LM Studio has no API to report model size or host memory usage, so you
provide rough estimates yourself. A not-loaded model is only let through if it (plus whatever's
already loaded) fits the budget; anything with an unknown size is rejected rather than risked:

```json
{
  "host_id": "gpu-box",
  "...": "...",
  "memory_budget_gb": 32,
  "model_sizes_gb": {
    "qwen2.5-coder-14b-instruct-mlx": 8.5,
    "google/gemma-4-e4b": 8.0
  }
}
```

There's no forced eviction — LM Studio has no unload API either, so this is a ceiling on what the
orchestrator will *trigger*, not an active memory manager. Over budget → `503
model_capacity_exceeded` (distinct from the flat `400` above — this one might work later if you
free something up manually).

## External node load visibility (optional)

`in_flight` on `GET /v1/nodes` only counts requests the orchestrator itself dispatched — it can't
see load on a node caused by something else hitting LM Studio directly (another tool, a manual
`curl`, LM Studio's own UI playground). If you install LM Studio's `lms` CLI on the machine
running the orchestrator, `GET /v1/nodes` and the TUI's `ext_load` column additionally report each
node's *actual* queue/status straight from LM Studio, regardless of who caused it:

```bash
curl -fsSL https://lmstudio.ai/cli/install.sh | bash
```

This is entirely optional — everything else works without it, and a missing or non-functional
`lms` binary just makes `external_load` report `unavailable` rather than failing any request. See
[docs/adr/0005-lms-cli-for-external-node-load-visibility.md](docs/adr/0005-lms-cli-for-external-node-load-visibility.md)
for why `lms` is required (LM Studio's REST API doesn't expose load/queue data itself).

## Configuration reference

| Var | Default | Purpose |
| --- | --- | --- |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | Gateway bind address |
| `ORCHESTRATOR_PORT` | `8080` | Gateway port |
| `ORCHESTRATOR_API_KEYS_FILE` | `./config/api_keys.json` | Client/key/authorization config |
| `ORCHESTRATOR_AUTH_ENABLED` | `true` | Set `false` to disable auth entirely (local testing only) |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234` | Default LM Studio host registered at startup |
| `LMSTUDIO_TIMEOUT` | `120` | Per-chunk read timeout to LM Studio (seconds) — see note below |
| `LMSTUDIO_CONNECT_TIMEOUT` | `10` | TCP connect timeout to LM Studio (seconds) — separate from `LMSTUDIO_TIMEOUT`, see note below |
| `LMSTUDIO_MAX_RETRIES` | `2` | Retry count for a connection failure before any chunk arrives — see note below |
| `LMSTUDIO_CONTEXT_WINDOW` | `8192` | Context window advertised for routing |
| `LMSTUDIO_MAX_CONCURRENT_REQUESTS` | `4` | Capacity used by the scheduling queue |
| `ORCHESTRATOR_DISPATCH_WAIT_TIMEOUT_S` | `120` | How long a queued request waits for a free host slot before failing with `503` |
| `ORCHESTRATOR_LMS_BINARY_PATH` | `lms` | Path to the optional `lms` CLI for external node load visibility — see below |
| `ORCHESTRATOR_EXTERNAL_LOAD_PROBE_INTERVAL_S` | `2` | Cache TTL (seconds) for external load probes — matches the TUI's default poll interval so its sparkline stays responsive; raise it if the `lms` subprocess overhead matters more than freshness for your setup |

**Long-running generations**: the orchestrator always talks to LM Studio via its streaming
protocol internally, even for a non-streaming caller — so `LMSTUDIO_TIMEOUT` is a *gap* timeout
(max silence between tokens), not a cap on total generation time. A response that's just slow to
produce (not stuck) is never retried: once the backend has started responding, a timeout means
"still generating," not "transient failure," and resending the same prompt would only compound
the wait. `LMSTUDIO_MAX_RETRIES` only covers a connection failure before any output has arrived at
all (e.g. LM Studio not running yet). If you still regularly see `504 backend_timeout` for very
long-form output, raise `LMSTUDIO_TIMEOUT` further — it only needs to cover the longest gap
between two tokens, not the whole response.

**Connect vs. gap timeout**: `LMSTUDIO_CONNECT_TIMEOUT` bounds only the initial TCP connect (is
this host even reachable?), kept short so a powered-off or network-unreachable host fails fast
instead of tying up `GET /health/ready` for the full `LMSTUDIO_TIMEOUT` duration. `LMSTUDIO_TIMEOUT`
still governs everything after the connection is established (the per-chunk gap). Raise
`LMSTUDIO_CONNECT_TIMEOUT` only if a legitimately slow network (e.g. Wi-Fi to another building)
makes a healthy host's connect attempt regularly exceed 10 seconds.

For genuinely long responses, also prefer **streaming** (`"stream": true` in the request body):
the client gets tokens as they're generated instead of waiting in silence for the whole response.
Note that the concurrency slot is held for a request's entire duration either way (streaming or
not) — a long generation still occupies one of `LMSTUDIO_MAX_CONCURRENT_REQUESTS` for as long as
it runs; register more hosts if you need to run many long generations at once.

## API surface

- `POST /v1/chat/completions` — OpenAI-compatible chat completions (streaming and non-streaming).
- `POST /v1/nodes/register`, `POST /v1/nodes/{host_id}/heartbeat`, `DELETE /v1/nodes/{host_id}`,
  `GET /v1/nodes` — manage model hosts.
- `GET /health/live`, `GET /health/ready` — liveness/readiness (no auth required).

## Terminal dashboard (TUI)

An optional terminal dashboard shows live node health, firing alerts, and queue/token usage —
comparable to `docker stats`. It's a separate, read-only client; it doesn't need to run on the
same machine as the orchestrator.

```bash
uv sync --extra tui
uv run llm-home-lab-tui --base-url http://localhost:8080 --api-key sk-dev-changeme
```

Add a client entry to `config/api_keys.json` scoped to what the dashboard reads:

```json
{
  "client_id": "tui-dashboard",
  "allowed_path_prefixes": ["/v1/nodes", "/v1/alerts"],
  "keys": [{"key": "sk-dev-changeme", "expires_at": null}]
}
```

`--base-url`/`ORCHESTRATOR_BASE_URL` and `--api-key`/`ORCHESTRATOR_API_KEY` are interchangeable
(flag or env var); `--interval` controls the poll frequency in seconds (default `2`). The Node
Load panel charts two sparklines per node: green for the orchestrator's own `in_flight` ratio,
orange for external load (busy/queued, from `lms`) when it's installed — see above.

## Demo workload script

`scripts/demo_workload.py` generates a burst of concurrent chat completion requests against the
orchestrator, for exercising and observing routing across multiple registered nodes — pair it
with the TUI dashboard (above) in another terminal to watch live request distribution:

```bash
uv run python scripts/demo_workload.py \
  --base-url http://localhost:8080 --api-key sk-dev-changeme \
  --model google/gemma-4-e4b --concurrency 8 --count 40
```

The orchestrator routes each request to the least-loaded healthy node (by current
`in_flight / max_concurrent_requests`), so with `--concurrency` above 1 you should see load
spread across all your registered nodes rather than piling onto just one — that's what to point
at in the TUI's node table. `--base-url`/`ORCHESTRATOR_BASE_URL` and
`--api-key`/`ORCHESTRATOR_API_KEY` work the same as for the TUI. `--model` must match a model
actually loaded/allowed on your nodes.

## How it works

- **Routing**: a policy-scored engine picks the best available host per request (e.g. lowest
  latency), with sticky sessions so a conversation stays on the same host.
- **Health**: hosts that fail health checks are excluded from routing until they recover.
- **Capacity**: requests queue (priority + fair scheduling across clients) when every eligible
  host is already at its concurrency limit, instead of failing outright.
- **Auth**: every client has its own key(s) and is restricted to the endpoints it needs; every
  decision is audit-logged.

Design docs and specs for each piece live in `docs/specs/` and `docs/plans/`, and project
planning/issue tracking lives in `.plan/` (synced to GitHub via Planhub) if you want the deeper
detail — none of that is needed to just run the thing.
