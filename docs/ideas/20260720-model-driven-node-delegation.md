# Model-driven delegation between nodes

## Status

draft

## Problem

Today the orchestrator only does server-side, per-request routing: a client sends one
`/v1/chat/completions` call, and `RoutingEngine.select_backend` picks exactly one backend host
for it. There is no way for the *model itself*, mid-conversation, to hand off a sub-task to
another registered node/model — a user talking to one local LLM (e.g. via LM Studio on a
MacBook) cannot have that model delegate part of the work to other LLMs in the cluster.

## Audience / Value

A user who normally talks to a single "front" model would get access to the whole cluster's
capabilities (larger/specialized models on other nodes) through that one conversation, instead
of manually switching `model`/`task_type` per request. Value is agent-style workflows (e.g. a
small fast model delegating a heavier sub-task to a bigger model on another node) without the
user having to orchestrate that by hand.

## Solution shape (not final)

Add OpenAI-style tool/function calling to the API, with a delegation tool (e.g.
`delegate_to_model`) that the front model can invoke. Executing that tool call issues a new
request — most likely back through the orchestrator's own `/v1/chat/completions` (reusing
routing, health, and capacity logic) rather than a new direct-to-host path — and feeds the
result back to the front model as a tool result message.

## Options

- **Delegate through the orchestrator** (re-enter `/v1/chat/completions` with a different
  `model`/`task_type`): reuses all existing routing/failover/capacity logic as-is; no new
  per-node dispatch path needed.
- **Delegate to an explicit `host_id`**: gives the model direct control over placement, but
  requires a new per-node inference path — none exists today (`/v1/nodes/*` is
  registration/heartbeat/observability only) — and works against the "agent never picks a
  backend" design principle below.
- **Tool call executed by the orchestrator vs. by the client**: either the orchestrator's proxy
  loop resolves tool calls internally (model never leaves the orchestrator's control), or the
  client SDK/agent loop resolves them by calling the orchestrator again itself. Affects where
  session/context threading for the sub-call lives.

## Constraints / Appetite

Genuinely new surface area, not a small addition: needs `tools`/`tool_calls` fields on
`ChatCompletionRequest`/response, `LMStudioBackend` support for passing tool definitions through
to LM Studio and parsing tool calls back, plus the delegation tool + execution loop itself.
Should not be scoped as a quick patch to the existing gateway.

## Rabbit holes

- This runs counter to the current design principle in `Local_LLM_Orchestrator_Concept.md`:
  *"the agent does not know where the model runs... all intelligence lives in the
  orchestrator"* / *"the orchestrator owns this decision so agents never have to pick a backend
  themselves."* If model-driven delegation is pursued, that principle needs to be explicitly
  revisited (likely worth an ADR), not silently contradicted.
- Recursive/looping delegation (a delegated model delegating again) needs a cycle/depth guard —
  undefined today, easy to end up with infinite fan-out or a delegation cycle across nodes.
- Session/context handling for a delegated sub-call: fork vs. share `session_id`, and whether
  delegated calls count against the same session's routing stickiness and capacity accounting.
- Tool-calling support in general (`docs/specs/20260711-openai-compatible-api-gateway.md`, open
  question) was explicitly left unresolved for M1 — this idea would resolve that question in the
  direction of "yes, and specifically for cross-node delegation," which is a bigger scope than a
  generic passthrough of the `tools` field.

## No-gos

- No direct client-to-specific-node inference path as part of this idea — delegation goes
  through the orchestrator's own routing unless a later decision explicitly changes that.
- No general-purpose arbitrary tool-calling passthrough (e.g. letting models call *any*
  client-defined tool) — scope here is specifically node/model delegation.

## Related

- Spec: [20260711-openai-compatible-api-gateway](../specs/20260711-openai-compatible-api-gateway.md)
  (open question on function/tool-calling scope, never resolved — this idea proposes resolving
  it for the delegation case specifically)
- ADR: [0004-persist-node-registry-no-auto-deregistration](../adr/0004-persist-node-registry-no-auto-deregistration.md),
  [0005-lms-cli-for-external-node-load-visibility](../adr/0005-lms-cli-for-external-node-load-visibility.md)
  (registry/observability building blocks a delegation tool would read from)
- Design doc: [Local_LLM_Orchestrator_Concept](../../Local_LLM_Orchestrator_Concept.md) (states
  the "agent never picks a backend" principle this idea would need to revisit)

## Open Questions

- Does pursuing this mean revisiting the "agent never picks a backend" principle via an ADR, or
  is delegation framed as a different layer (front model picks *what* to delegate, orchestrator
  still picks *where* it runs)?
- Delegated call routing: reuse `/v1/chat/completions` end-to-end, or add a narrower internal
  path for tool-originated calls (e.g. skipping some client-facing validation)?
- How is delegation depth/cycles bounded?
- Does a delegated sub-call share the parent's `session_id` (and its routing stickiness/capacity
  accounting), or always start a fresh session?
