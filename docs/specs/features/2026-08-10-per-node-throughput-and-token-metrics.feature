Feature: Per-node throughput and token-size metrics
  Operators can see, per node, how many prompts it has completed, how fast, and how
  large those prompts and completions were — without grepping logs.

  # Related spec: docs/specs/2026-08-10-per-node-throughput-and-token-metrics.md

  Scenario: A host with no recorded completions reports unavailable, not zero-division
    Given a host "fresh-host" is registered with no completions recorded yet
    When the metrics snapshot is computed for "fresh-host"
    Then completions_total for "fresh-host" is 0
    And avg_latency_ms for "fresh-host" is unavailable
    And prompt_tokens avg/min/max for "fresh-host" are unavailable

  Scenario: A healthy completion updates the host's counters
    Given a host "host-a" is registered
    When a healthy completion is recorded for "host-a" with latency_ms 120, prompt_tokens 50, and completion_tokens 20
    Then completions_total for "host-a" is 1
    And avg_latency_ms for "host-a" is 120
    And avg_prompt_tokens for "host-a" is 50
    And avg_completion_tokens for "host-a" is 20

  Scenario: A degenerate completion does not affect the metrics
    Given a host "host-b" is registered
    When a degenerate completion (empty content) is attempted for "host-b" with prompt_tokens 50
    Then completions_total for "host-b" is 0
    And avg_prompt_tokens for "host-b" is unavailable

  Scenario: A backend error does not affect the metrics
    Given a host "host-c" is registered
    When a completion attempt for "host-c" raises a backend error
    Then completions_total for "host-c" is 0

  Scenario: Averages and min/max accumulate across multiple healthy completions
    Given a host "host-d" is registered
    When a healthy completion is recorded for "host-d" with prompt_tokens 100
    And a healthy completion is recorded for "host-d" with prompt_tokens 300
    Then completions_total for "host-d" is 2
    And avg_prompt_tokens for "host-d" is 200
    And prompt_tokens_min for "host-d" is 100
    And prompt_tokens_max for "host-d" is 300

  Scenario: Recorded latency excludes time spent waiting in the scheduling queue
    Given a host "host-e" is registered and currently saturated
    When a request queues for 500 ms waiting for a free slot on "host-e"
    And the eventual backend call on "host-e" itself takes 80 ms
    Then the latency recorded for "host-e" is 80, not 580
