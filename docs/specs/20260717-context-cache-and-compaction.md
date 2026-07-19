# Context Cache and Compaction Strategy

## Status

draft

## Summary

A pure context-assembly function that turns a [`Session`](../../src/llm_home_lab/state/models.py)
(messages plus an optional summary, as returned by
[`SessionManager.read_session`](../../src/llm_home_lab/state/session_manager.py)) into the
message list a backend request would send, applying a token-budget-aware selective-retrieval and
summarization-fallback strategy when the full session doesn't fit. A `ContextCache` wraps that
function, keyed by a hashable key derived from session content plus the token budget, so
repeated assembly for an unchanged session — for example, the same session retried against a
second backend after [failover](20260717-failover-and-health-policy.md) — doesn't re-run the assembly
work. This delivers the module only, the same way
[session-manager-core](20260717-session-manager-core.md) shipped `SessionManager` without wiring
`session_id` into `/v1/chat/completions` — gateway adoption is still that spec's deferred
decision, not this one's.

## User stories

- As the orchestrator, I want an assembled context that always fits within a request's token
  budget, so that oversized session history never produces a request the backend rejects.
- As the orchestrator, I want repeated assembly of an unchanged session (e.g. retrying the same
  request against a different backend after failover) to skip redundant work, so that failover
  latency doesn't compound with reassembly cost.
- As an operator, I want cache hit/miss and compaction-trigger counts exposed, so I can see how
  often sessions are being truncated and how effective the cache is.
- As a test author, I want compaction decisions driven purely by the `Session` object and a
  token budget, not the wall clock or randomness, so behavior is deterministic in tests.

## Requirements

- Provide `assemble_context(session: Session, token_budget: int) -> AssembledContext` — a pure
  function (no I/O, no cache) that converts `session.summary` (if present) into a leading
  `role="system"` message and `session.messages` into `role`/`content` messages in `seq` order.
- If the full assembled message list's estimated token count is within `token_budget`, return it
  unchanged: `AssembledContext(messages=..., compacted=False, dropped_message_count=0)`.
- If it exceeds `token_budget`, apply **selective retrieval**: keep the summary message (if any)
  and as many of the most recent raw messages as fit in the remaining budget, working backwards
  from the end of `session.messages`, preserving their original order in the result.
- **Never drop below the single most recent message**, even if it alone exceeds the remaining
  budget — an active task's latest turn is never sacrificed to a budget constraint. This is the
  spec's answer to "compaction does not remove required facts for active tasks": the summary (an
  explicit, caller-vetted fact set) and the latest turn are the two things compaction never
  drops.
- `AssembledContext.compacted` is `True` whenever `dropped_message_count > 0`, `False` otherwise
  (not inferred from which code branch ran, so an edge case that drops nothing is never
  misreported as compacted).
- Provide `ContextCache` wrapping `assemble_context`: `ContextCache.assemble(session,
  token_budget) -> AssembledContext`, keyed by a hashable tuple derived from `session.session_id`,
  each message's `(seq, role, content)`, the summary's `(covers_up_to_seq, summary_text)` if
  present, and `token_budget` — so any change to session content or budget is a different key,
  never a stale hit.
- `ContextCache` tracks `hits`, `misses`, and `compaction_count` (incremented once per miss where
  the result was compacted) as public counters, plus a `hit_ratio` property (`hits / (hits +
  misses)`, `0.0` when nothing has been recorded yet).
- `ContextCache` is size-bounded (`max_entries`, default 128) with least-recently-used eviction,
  consistent with this codebase's convention of bounding state rather than growing it unbounded
  (see [workspace-state-capture](20260717-workspace-state-capture.md)).

## Behavior

**Fits under budget**: a session whose assembled messages estimate at or under `token_budget`
comes back unchanged, `compacted=False`, `dropped_message_count=0`.

**Repeat assembly of the same session state is a cache hit**: calling `ContextCache.assemble`
twice with an unchanged `session` (same messages, same summary) and the same `token_budget`
records a hit the second time and returns an equal `AssembledContext` without needing
`assemble_context` to be invoked again.

**A changed session is a cache miss, never a stale hit**: appending a message (or updating the
summary) between two calls changes the cache key, so the second call misses and reassembles —
callers never see history from before a change.

**Oversized session selects the most recent messages that fit**: when the full assembly exceeds
`token_budget`, the result keeps the summary (if any) plus the longest suffix of
`session.messages` whose estimated tokens fit in what's left of the budget, in original order.
`compacted=True` and `dropped_message_count` equals the number of older messages left out.

**The latest message is never dropped, even if it alone exceeds the budget**: a single very long
most-recent message is still included by itself, over budget, rather than being cut — matching
the requirement's "active tasks" guarantee.

**The summary is never dropped by compaction**: whenever `session.summary` is present, it is
part of the result regardless of how aggressively selective retrieval had to truncate the raw
messages.

**Compaction and cache metrics accumulate independently**: `hit_ratio` reflects hit/miss counts
across all calls; `compaction_count` only increments on a miss whose result was compacted (a
cache hit for an already-compacted entry does not double-count).

**Cache eviction is least-recently-used**: once distinct session-state keys exceed
`max_entries`, the least-recently-accessed entry is evicted first, so a long-running orchestrator
doesn't grow this cache unboundedly.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/features/20260717-context-cache-and-compaction.feature`.

## Related

- Spec: [session-manager-core](20260717-session-manager-core.md) — the `Session`/`StoredMessage`/`Summary`
  shapes this module consumes; also the source of the still-open "when does the gateway adopt
  `session_id`" question this spec does not resolve either.
- Spec: [failover-and-health-policy](20260717-failover-and-health-policy.md) — the failover-retry scenario
  motivating the cache (reassembling the same session against a fallback backend).
- Plan: [context-cache-and-compaction](../plans/20260717-context-cache-and-compaction.md)
- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M3 — context cache and
  compaction)
- Issue: `.plan/milestones/m3-routing-and-reliability/issues/issue-003-context-cache-and-compaction.md`
  (#9)
- Acceptance: `docs/specs/features/20260717-context-cache-and-compaction.feature`

## Open Questions

- Gateway adoption — how `/v1/chat/completions` would call `SessionManager.read_session` and
  `ContextCache.assemble` to build the outgoing `messages` from `session_id`, instead of the
  client supplying `messages` directly as today — remains
  [session-manager-core](20260717-session-manager-core.md)'s deferred decision, not resolved here.
  Without that wiring, this module is proven correct in isolation but not yet exercised
  end-to-end through the API.
- Without a summary, selective retrieval truncates by recency alone — there is no guarantee that
  older, still-relevant facts aren't silently dropped; the spec relies on the caller having
  summarized before context grows that large. Whether that's an acceptable guarantee or needs a
  harder backstop (e.g. refusing to truncate below a floor without a summary) is left open.
- "Benchmark" acceptance criteria (median context-assembly time, latency impact) are not
  something a unit test meaningfully asserts; this spec treats "the cache demonstrably skips
  reassembly on an unchanged key" (see Behavior) as the testable proxy for that criterion.
