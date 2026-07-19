# Context Cache and Compaction Strategy Plan

## Status

draft

## Related

- Spec: [context-cache-and-compaction](../specs/20260717-context-cache-and-compaction.md)
- Depends on: [session-manager-core](../specs/20260717-session-manager-core.md) and
  [`state/models.py`](../../src/llm_home_lab/state/models.py) (`Session`, `StoredMessage`,
  `Summary` — the shapes this module consumes)
- Related: [failover-and-health-policy](../specs/20260717-failover-and-health-policy.md) (the failover-
  retry scenario motivating the cache)
- Issue: #9 (Add context cache and compaction strategy, M3)

## Scope

Build `assemble_context` (pure, session-in/messages-out, budget-aware selective retrieval and
summarization fallback) and `ContextCache` (a bounded, keyed, metrics-tracking wrapper around
it). Ship the module standalone and fully tested, the same way `SessionManager` shipped in M2
without gateway wiring.

Out of scope for this plan (per the spec's Open Questions):

- Wiring `session_id` into `/v1/chat/completions` so the gateway actually calls
  `SessionManager.read_session` + `ContextCache.assemble` to build outgoing `messages` — that
  remains session-manager-core's deferred decision, untouched by this plan. `api/app.py` and
  `main.py` are not modified.
- Any backstop for truncating below a floor when no summary exists — selective retrieval by
  recency alone is what ships; a harder guarantee is left open.
- Literal latency benchmarking — the cache's effectiveness is proven via cache-hit tests, not a
  timing harness.

## Steps

1. **Context package** (`src/llm_home_lab/context/`, new package sibling to `backends/`,
   `routing/`, `health/`, `state/`) — `__init__.py` exporting the public surface.
2. **`AssembledContext` model** (`src/llm_home_lab/context/models.py`) — `messages:
   list[Message]` (reusing `llm_home_lab.api.models.Message`, the existing wire format —  no new
   message type), `compacted: bool`, `dropped_message_count: int`.
3. **`assemble_context`** (`src/llm_home_lab/context/assembler.py`) — converts
   `session.summary` into a leading `role="system"` message when present, then
   `session.messages` into `Message`s in `seq` order; if the estimated token total (reusing the
   same chars-over-4 heuristic style as
   [`routing/engine.py`](../../src/llm_home_lab/routing/engine.py)'s `_estimate_token_budget`,
   duplicated locally rather than shared — it's two lines, not worth a cross-module dependency)
   is within `token_budget`, returns it as-is; otherwise keeps the summary message plus the
   longest suffix of recent messages that fits (working backwards, never dropping below the last
   message even if it alone is over budget), setting `compacted` from whether anything was
   actually dropped.
4. **`ContextCache`** (`src/llm_home_lab/context/cache.py`) — `ContextCache(max_entries=128)`:
   `assemble(session, token_budget)` builds a hashable key from `(session_id, tuple of (seq,
   role, content) per message, (covers_up_to_seq, summary_text) or None, token_budget)`, checks
   an `OrderedDict`-backed LRU store, calls `assemble_context` on miss, records `hits`/`misses`/
   `compaction_count`, and evicts the least-recently-used entry past `max_entries`.
5. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
   .`, `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill): one test from the spec's behavior/acceptance scenarios at a
time, red → green, then a refactor pass once all scenarios pass.

## Risks

- **Cache key completeness**: if the key omits any part of `Session` state that affects assembly
  output (e.g. summary text but not `covers_up_to_seq`), two different sessions could collide on
  one cache entry and serve stale content. Cover this with an explicit "changed session is a
  cache miss" test for each field the key includes, not just one generic mutation test.
- **Off-by-one on "never drop the last message"**: the suffix-selection loop must special-case
  the first message it considers (always keep it) separately from the general "does it still
  fit" check for every later one — getting this backwards either drops the most recent turn under
  a very tight budget or silently keeps unbounded messages. Test the exact boundary (budget below
  the single most recent message's cost) explicitly.
- **LRU eviction correctness**: eviction must count *distinct keys*, not calls — a cache hit
  should refresh recency without counting as a new entry. A test that hits an existing key
  before exceeding `max_entries` should confirm that entry, not some other, survives.

## Open Questions

- Same as the spec's: gateway adoption of `session_id`, whether recency-only truncation needs a
  harder floor guarantee, and benchmark-style acceptance criteria are deferred, not blocking this
  plan.
