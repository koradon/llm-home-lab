# Completion quality health signal

## Status

completed

## Related

- Idea: [completion-quality-health-signal](../ideas/20260727-completion-quality-health-signal.md)
  (Option A — the smallest change that closes the actual correctness gap)
- Spec: [failover-and-health-policy](../specs/20260717-failover-and-health-policy.md) —
  `HealthMonitor.record_probe()`/state machine, reused as-is
- Issue: #50 — feed real completion outcomes into `HealthMonitor` (Option A)
- Blocks: #51 (surface per-host failure counts), #52 (factor failure rate into routing) — both
  depend on `HealthMonitor` actually seeing real completion failures first

## Scope

`HealthMonitor` currently only hears from the periodic background poller's `check_health()` calls
(`GET /v1/models`) — a liveness probe that says nothing about whether the loaded model produces
usable completions. This plan wires the two real request paths — `chat_completions()`'s
non-streaming branch and `_stream_chunks()` — into `health_monitor.record_probe()`, so a host that
passes every liveness probe while returning degenerate completions (empty/whitespace content, or a
`finish_reason` other than `"stop"`) still gets excluded by the existing
failure-threshold/cooldown/recovery state machine.

Also in scope (approved beyond issue #50's literal text, during plan review): feeding
`record_probe(healthy=False, ...)` from the `except BackendError` branch of the non-streaming path
— an outright backend error between liveness polls is at least as clear an unhealthy signal as
empty content, and the request path currently has no way to record it at all.

Out of scope: any change to `HealthMonitor` itself (`src/llm_home_lab/health/monitor.py`) — its
state machine already does the right thing once fed real signal. Also out of scope: surfacing
failure counts via `/v1/nodes`/TUI (#51) and folding `health_score()` into routing weight (#52) —
both are separate, deliberately sequenced follow-ups.

## Steps

1. **Degenerate-completion helper.** Add a free function in `src/llm_home_lab/api/app.py`:

   ```python
   def _is_degenerate_completion(content: str, finish_reason: str | None) -> bool:
       return not content.strip() or finish_reason != "stop"
   ```

2. **Non-streaming success path** (`chat_completions()`, current lines ~559–572). After
   `result = await backend.complete(request)` returns successfully, call:

   ```python
   health_monitor.record_probe(
       decision.backend_id,
       healthy=not _is_degenerate_completion(result.content, result.finish_reason),
       at=datetime.now(UTC),
   )
   ```

   Capture `at` freshly at this point (matching the file's existing style of calling
   `datetime.now(UTC)` inline at each use site, e.g. the adjacent
   `metrics_registry.record_failover_outcome(...)` calls) rather than reusing the `now` captured at
   the top of the request handler.

3. **Non-streaming exception path** (same `try`, the `except BackendError:` branch). Before
   re-raising, call `health_monitor.record_probe(decision.backend_id, healthy=False,
   at=datetime.now(UTC))`.

4. **Streaming path** (`_stream_chunks()`, current lines ~597–635). Add a `health_monitor:
   HealthMonitor` parameter. Across the `async for chunk in backend.stream(request)` loop, track:
   - `saw_content: bool` — set `True` the first time a chunk's `chunk.content.strip()` is
     non-empty.
   - `last_finish_reason: str | None` — updated whenever `chunk.finish_reason is not None`.

   After the loop completes normally (i.e. the stream finished without raising), call:

   ```python
   health_monitor.record_probe(
       backend_id, healthy=saw_content and last_finish_reason == "stop", at=datetime.now(UTC)
   )
   ```

   Update the call site in `chat_completions()`'s inner `_chunks()` function (current lines
   ~546–548) to pass `health_monitor` through to `_stream_chunks(...)`.

   A stream that raises mid-way (i.e. `backend.stream()` itself errors) is out of scope for this
   plan — same boundary the idea doc drew; revisit only if it turns out to matter in practice.

5. **Tests — appended to `tests/test_failover.py`** (not a new file), reusing the existing
   `FakeBackend` and `_app_for(*backends, failure_threshold=1)` helpers already there. Extend
   `FakeBackend` as needed (e.g. a way to make `complete()` return degenerate content or raise, and
   a `stream()` method) without breaking the two existing tests in that file. TDD, one behavior at
   a time:
   - A backend whose `complete()` returns empty content `failure_threshold` times in a row gets
     excluded; the next request reroutes to a healthy backend. This is issue #50's explicitly
     named regression test.
   - A backend whose `complete()` returns non-empty content but `finish_reason="length"` is also
     excluded after threshold — content alone isn't enough.
   - A backend whose `complete()` raises `BackendError` repeatedly gets excluded via the exception-
     path probe (step 3), without needing a periodic `/v1/models` failure either.
   - A streaming backend whose `stream()` yields only empty-content chunks gets excluded the same
     way as the non-streaming case.

6. **Verification** — `uv run pytest --cov=llm_home_lab`, `uv run ruff check .`, `uv run ruff
   format --check .`, `uv run mypy src`.

## Risks

- **False-positive failovers from a legitimately short-but-correct completion.** Mitigated by
  using the conservative definition from the idea doc (empty/whitespace content or non-`"stop"`
  finish_reason) rather than a length heuristic — a real one-word correct answer with
  `finish_reason="stop"` is never flagged.
- **Streaming health-signal blind spot if the loop is short-circuited.** The `saw_content`/
  `last_finish_reason` tracking must live across the whole `async for` loop, not just the final
  chunk — a chunk stream that front-loads content then ends on a non-`"stop"` reason (e.g. cut off
  by a length limit) must still be flagged degenerate.
- **Widening scope past the issue's literal text (the exception-path probe).** Flagged explicitly
  in Scope above so it doesn't read as silent scope creep — this was a deliberate decision made
  during plan review, not an oversight.

## Open Questions

- None — the idea doc's open questions (degenerate-completion definition, whether Option C is
  needed) are resolved: conservative definition per Scope above; Option C (#52) stays deferred
  until Options A/B are observed in practice.
