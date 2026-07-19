Feature: Context cache and compaction strategy
  The orchestrator assembles a session's messages into a budget-fitting context, falling back to
  selective retrieval when the full history doesn't fit, and caches repeated assembly of an
  unchanged session so retries and failover don't pay for reassembly twice.

  # Related spec: docs/specs/20260717-context-cache-and-compaction.md

  Scenario: A session that fits under budget is returned unchanged
    Given a session whose assembled messages estimate at or under the token budget
    When the context is assembled
    Then the result is not compacted and no messages are dropped

  Scenario: Repeat assembly of an unchanged session is a cache hit
    Given a session has already been assembled once for a token budget
    When the same session and token budget are assembled again
    Then the cache reports a hit and returns an equal assembled context

  Scenario: A changed session is a cache miss
    Given a session has already been assembled once for a token budget
    When a new message is appended to the session and it is assembled again for the same budget
    Then the cache reports a miss and reassembles

  Scenario: An oversized session selects the most recent messages that fit
    Given a session whose full assembly exceeds the token budget
    When the context is assembled
    Then the result is compacted
    And it contains the most recent messages that fit within the budget in their original order

  Scenario: The single most recent message is never dropped even if it exceeds the budget alone
    Given a session whose most recent message alone exceeds the token budget
    When the context is assembled
    Then the result still contains that most recent message

  Scenario: The summary is never dropped by compaction
    Given a session with a summary and messages that together exceed the token budget
    When the context is assembled
    Then the result still contains the summary message

  Scenario: Compaction count only increments on a compacted cache miss
    Given a session whose assembly is compacted
    When it is assembled once and then assembled again unchanged
    Then the compaction count increments only once

  Scenario: The cache evicts least-recently-used entries once past its size bound
    Given the cache is at its maximum number of distinct session-state entries
    When a new distinct session state is assembled
    Then the least-recently-accessed entry is evicted
