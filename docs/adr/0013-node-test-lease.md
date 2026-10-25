# Lease a node with a heartbeat and a hard ceiling

## Status

accepted

## Context and Problem Statement

People and agents sometimes need to send real requests to one local model while other work
continues. Taking that node out of ordinary routing by hand is easy to forget, and a crashed
agent can block the node for hours. A single 30 minute timer is too short for the longer
Tripper checks. The node also has to stay reachable for the test itself.

## Considered Options

- One long timer, set to the whole test up front.
- A short lease renewed by a heartbeat, plus a separate hard ceiling on the total hold.
- Queue a reservation until a node is free.
- Refuse immediately when a reservation would starve ordinary routing.
- Send the test straight to the node's own address while the orchestrator skips that node.
- Keep the test on the orchestrator, and pin it with a lease the orchestrator checks.

## Decision Outcome

Chosen option: a short lease renewed by a heartbeat, with a configurable hard ceiling, refused
rather than queued, and pinned through the orchestrator.

The lease lasts 3 minutes unless `ORCHESTRATOR_LEASE_TTL_S` says otherwise. `llm-home-lab-run`
renews it about once a minute while the wrapped command is alive, so a crashed or stuck holder
frees the node within one lease. The total hold defaults to 3 hours
(`ORCHESTRATOR_LEASE_CEILING_S`). A reservation may ask for a shorter or longer ceiling, up to
`ORCHESTRATOR_LEASE_MAX_CEILING_S` (default 3 hours, the same as the default ceiling). A human
can force-release at any time.

A reservation is refused, not queued, when no suitable node is free or when granting it would
leave fewer than `ORCHESTRATOR_LEASE_MIN_ACTIVE_NODES` (default 1) healthy nodes in ordinary
routing. The refusal names every node, who holds it, and until when.

Test traffic keeps using the orchestrator. `llm-home-lab-run` exports `ORCHESTRATOR_BASE_URL`
and `OPENAI_BASE_URL` as one leased base that ends in `/v1` and carries the token in the path
(`/lease/<token>`). An unmodified client that only honours that base, and sends no custom
header, is routed to the leased node. `/v1/chat/completions` and `/capacity` both resolve under
that prefix, and so does `/v1/capacity`. A request that instead carries `X-LLM-Node-Lease` is
still routed to that leased node and no other. A local forwarding proxy was the other way to
inject the header; the path keeps streaming and API-key checks on the orchestrator itself.
Ordinary requests skip leased nodes. In-flight requests are not cancelled. Model matching is
unchanged.

Leases live in memory. An orchestrator restart drops them, which returns every node to routing
instead of leaving a hold behind with no heartbeat.

### Consequences

- Good, because a forgotten agent loses the node after one short lease, while a live test can
  still run up to the ceiling.
- Good, because production curation keeps a configurable minimum of nodes in ordinary routing,
  and the caller learns immediately who to wait on.
- Good, because tests do not need a node's address, base URL, or backend type.
- Bad, because a holder must keep the heartbeat running. A command that is not wrapped will not
  renew, and the node returns to routing while the command may still be going.
- Bad, because leases do not survive a restart. A test in progress has to reserve again.

## Related

- Spec: [node test lease](../specs/20261025-node-test-lease.md)
- Issue: https://github.com/koradon/llm-home-lab/issues/82
