---
title: Add context cache and compaction strategy
labels:
- performance
- cache
- memory
state: open
number: 9
state_reason: null
---

## Why

Repeatedly sending full context across model switches increases latency and cost.

## Scope

- Add prompt fragment cache keyed by session state hash.
- Implement selective context retrieval and summarization fallback.
- Measure cache hit ratio and impact on end-to-end latency.

## Acceptance Criteria

- Cache reduces median context assembly time in benchmark runs.
- Compaction does not remove required facts for active tasks.
- Metrics expose cache hit/miss and compaction trigger frequency.
