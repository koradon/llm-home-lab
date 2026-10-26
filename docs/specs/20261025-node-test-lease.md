# Node test lease

## Status

accepted

## Summary

A holder can reserve one registered node for a real-LLM check. While the lease is live the node
leaves ordinary routing. Requests that carry the lease are served by that node only, still
through the orchestrator. `llm-home-lab-run` reserves, heartbeats, and releases around one
command. The TUI shows the holder and the hard ceiling.

## User stories

- As an agent, I want to wrap one command so a real model is mine for that command only, so that
  I do not have to switch nodes off by hand.
- As an operator, I want a crashed reservation to end on its own, so that one forgotten test
  cannot starve curation.
- As an operator, I want the dashboard to show who has a node and until when, in a colour that
  is not online or offline.

## Requirements

- A node has at most one lease. A second reservation of that node is refused, including a repeat
  by the same holder.
- Only the lease token can pin traffic to the node or release it. Force-release does not need
  the token.
- Ordinary `/v1/chat/completions` traffic does not select a leased node. In-flight requests
  already on that node finish.
- A request with header `X-LLM-Node-Lease` is routed to the leased node and nowhere else. The
  same pin is a base URL `{origin}/lease/{token}/v1`: a client that only uses that base, and
  sends no lease header, is routed to the leased node. Model matching is the same check the
  gateway already uses. A model the leased node does not serve is rejected rather than sent to
  another node.
- If no node can be reserved without leaving fewer than the configured minimum of healthy,
  unleased nodes in routing, the reservation fails at once. The body lists every node, its
  status, the holder or `holder=free`, and for a hold the lease expiry and the hard ceiling.
- Default lease length is 3 minutes. The wrapper renews it every 60 seconds. Default hard
  ceiling is 3 hours. A reservation may set `ceiling_s` up to the server maximum, which defaults
  to 3 hours and is configurable on its own.
- Automated tests of this feature use fake backends. They do not call LM Studio or any live node.

## Behavior

### Reservation

`POST /v1/node-leases` with `{"holder": "...", "host_id": null, "model": null, "ceiling_s": null}`.

`holder` is required and must not be blank. `host_id` reserves that node. Without it, the server
picks the lowest `host_id` among healthy, unleased nodes that serve `model` (or any healthy
unleased node when `model` is omitted). An explicit `host_id` may name an offline node. Auto
selection does not.

Success is `201` and includes `token`, `host_id`, `holder`, `expires_at`, `hard_expires_at`,
and `expiring_soon`. The token is not repeated on `GET /v1/nodes`.

`expires_at` is one ttl after the lease is recorded, capped by the hard ceiling. The clock is
read after model discovery, immediately before the reservation is stored, so a slow discovery
does not return a token that is already past its ttl. A heartbeat sets `expires_at` forward by
one ttl again, never past the ceiling. When `now` reaches `expires_at` or `hard_expires_at`,
the lease is gone and the node is back in ordinary routing.

Refusal is `409` `lease_refused`. An unknown `host_id` is `404`. A ceiling above the server
maximum, or an empty holder, is `400`.

`POST /v1/node-leases/heartbeat` and `DELETE /v1/node-leases` require `X-LLM-Node-Lease`. A
wrong or unknown token is `403`. An ended lease is `409` `lease_ended` on heartbeat and on
release, and that call drops it.

`POST /v1/node-leases/force-release` with `{"host_id": "..."}` drops a live lease without the
token. A host with no live lease is `404`. This is the human override. It still requires an API
key allowed on `/v1/node-leases`, like every other lease route.

`GET /v1/nodes` adds `lease` on each node: `null`, or `holder`, `expires_at`,
`hard_expires_at`, and `expiring_soon`. `expiring_soon` is true when the sooner of the two
timestamps is within 60 seconds. The response never includes the token.

A key whose `allowed_path_prefixes` is only `/v1/chat/completions` can send a leased completion
but cannot reserve. A prefix of `/v1/` covers both.

### Completions

Without a lease header or leased URL, candidate hosts are the usual model matches minus leased
hosts. If that set is empty because every match is leased, the gateway returns `503` and does not queue.
Otherwise an ordinary request still waits for capacity, but only on hosts it is allowed to use.
A free slot on a leased node does not unblock it.

With the header, or with the token carried in `/lease/{token}/...`, the only candidate is that
node, and only if it serves the model. Capacity and health waits apply to that node alone.
The prefix is not an API key. A missing or invalid key is still rejected, and a key allowed
only on `/v1/chat/completions` can complete through the leased base but cannot reserve.
`/lease/{token}/v1/chat/completions`, `/lease/{token}/v1/capacity`, and `/lease/{token}/capacity`
accept POST, GET, and GET respectively. Every other path or method under `/lease/{token}/`
returns 404 without rewriting or executing the underlying route. Streaming is the same
response as the unprefixed route.

Allowed hosts are read again when the request is admitted and takes a slot. An ordinary request
that waited is not sent to a host reserved during the wait. A pinned request does not start
after its lease has expired or been force-released, including when another holder has reserved
that node. A queue poll admits only its own waiter; an earlier waiter stays queued.

### Wrapper

`llm-home-lab-run` is the agent entry point.

```bash
uv run llm-home-lab-run --node gpu-box -- uv run pytest tests/test_live_model.py
uv run llm-home-lab-run --model google/gemma-4-e4b -- python check.py
```

Flags: `--base-url` / `ORCHESTRATOR_BASE_URL`, `--api-key` / `ORCHESTRATOR_API_KEY`,
`--holder` / `LLM_HOME_LAB_HOLDER` (default `pid-<pid>`), `--node`, `--model`,
`--ceiling-seconds`, `--heartbeat-seconds` (default 60). The command follows `--`.

On reserve, stderr prints
`llm-home-lab-run: reserved <host> holder=<holder> until <expires_at> ceiling <hard_expires_at>`.
On release, stderr prints `llm-home-lab-run: released <host>`. Stdout is the command's own.

The child environment is the parent environment plus:

| Variable | Value |
| --- | --- |
| `ORCHESTRATOR_BASE_URL` | `{origin}/lease/{token}/v1` |
| `OPENAI_BASE_URL` | the same leased base |
| `LLM_HOME_LAB_NODE_LEASE` | lease token |
| `LLM_HOME_LAB_LEASE_HEADER` | `X-LLM-Node-Lease` |
| `LLM_HOME_LAB_NODE_ID` | reserved `host_id` |

The child uses `ORCHESTRATOR_BASE_URL` or `OPENAI_BASE_URL` as its base and does not append
another `/v1`. A client that posts to `{base}/chat/completions` and reads `{base}/capacity`
lands on the reserved node without sending `X-LLM-Node-Lease`. A client that does send that
header to the normal origin still pins. The wrapper does not intercept the child's sockets.
`--base-url` accepts the orchestrator origin or a base that already ends in `/v1`.

From another repository, without a global install:

```bash
uv run --project "$LLM_HOME_LAB_REPO" -- llm-home-lab-run --node "$NODE" -- <command>
```

`LLM_HOME_LAB_REPO` is the llm-home-lab checkout. The wrapper reads `ORCHESTRATOR_BASE_URL`
and `ORCHESTRATOR_API_KEY` from the environment it inherits. Do not install it with `uv tool`
or `pipx`.

Exit codes:

| Code | Meaning |
| --- | --- |
| command's own code | the command ran to completion, including failure |
| 2 | reservation refused, missing command, or argparse usage. The command did not start. Stderr starts with `llm-home-lab-run: refused:` for a refusal. |
| 3 | orchestrator unreachable, or another error that is not a refusal, before the command starts |
| 4 | the lease ended or was rejected while the command was running. The command is terminated, then the lease is released. |
| 127 | the command could not be started. The lease is still released. |

Release runs on success, failure, a lost lease, and interrupt. A command interrupted with
SIGINT is signalled, and the wrapper returns the child's code (130 when the child exits on
that signal).

### TUI

The Nodes status cell:

| Situation | Plain text | Colour |
| --- | --- | --- |
| online, no lease | `online <in_flight>/<capacity>` | status word bold green, as before |
| reserved, online | `reserved <holder> until <hard ceiling> <in_flight>/<capacity>` | `reserved` is `#c000ff` |
| expiring, online | `expiring <holder> until <hard ceiling> ...` | `expiring` is `#ff8800` |
| offline, no lease | `offline <in_flight>/<capacity>` | bold red, as before |
| reserved and offline | `offline reserved <holder> until <hard ceiling> ...` | `offline` stays red and `reserved` stays `#c000ff` |

`until` is the hard ceiling, the latest the hold can last. `expiring` means the short lease or
the ceiling has 60 seconds or less left, which is what a missed heartbeat looks like before the
node returns to routing.

A colour export of all five states is in
[node-lease-states.svg](assets/node-lease-states.svg).

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ORCHESTRATOR_LEASE_TTL_S` | `180` | short lease, renewed by heartbeat |
| `ORCHESTRATOR_LEASE_CEILING_S` | `10800` | hard ceiling when the reservation omits `ceiling_s` |
| `ORCHESTRATOR_LEASE_MAX_CEILING_S` | `10800` | longest `ceiling_s` a reservation may request. Must be >= the default ceiling. |
| `ORCHESTRATOR_LEASE_MIN_ACTIVE_NODES` | `1` | healthy unleased nodes that must remain in ordinary routing |

## Acceptance scenarios (BDD)

See `docs/specs/features/20261025-node-test-lease.feature`.

## Related

- ADR: [0013-node-test-lease](../adr/0013-node-test-lease.md)
- Spec: [TUI operator dashboard](20260719-tui-operator-dashboard.md)
- Issue: https://github.com/koradon/llm-home-lab/issues/82
- Acceptance: `docs/specs/features/20261025-node-test-lease.feature`

## Open Questions

- None. Tests talk to the orchestrator, not to the node.
