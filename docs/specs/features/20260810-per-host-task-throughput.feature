Feature: Per-host task throughput and busy/total slot display
  Operators see how many slots are busy and how many tasks each host processes per hour.

  # Related spec: docs/specs/20260810-per-host-task-throughput.md

  Scenario: A llama-server host shows busy over total slots
    Given a llama-server host reports 7 of 8 slots processing
    When the TUI renders the Nodes table
    Then the ext_load cell reads "busy (7/8 slots)"

  Scenario: An LM Studio host still shows a true queue count
    Given an LM Studio host reports status "processingPrompt" with 2 queued
    When the TUI renders the Nodes table
    Then the ext_load cell reads "processingPrompt (2 queued)"

  Scenario: Tasks per hour counts completions in the window
    Given the window is 1 hour
    And "host-a" completed 3 healthy tasks in the last hour
    When throughput is computed for "host-a"
    Then tasks_per_hour is 3.0

  Scenario: A completion exactly one window old is excluded
    Given "host-a" completed a task exactly 1 hour ago
    When throughput is computed for "host-a"
    Then tasks_per_hour is 0.0

  Scenario: Tasks per hour per slot divides by the slot count
    Given "host-a" has 8 slots and 16 tasks per hour
    When throughput is computed for "host-a"
    Then tasks_per_hour_per_slot is 2.0

  Scenario: No completions does not divide by zero
    Given "host-a" has 8 slots and no completions in the window
    When throughput is computed for "host-a"
    Then tasks_per_hour is 0.0
    And tasks_per_hour_per_slot is 0.0

  Scenario: An unknown slot count reports n/a
    Given "host-a" has an unknown slot count
    When the TUI renders the Nodes table
    Then the tasks/h/slot cell reads "n/a"

  Scenario: Failed and degenerate completions are not counted
    Given a request to "host-a" fails with a backend error
    And another returns empty content
    When throughput is computed for "host-a"
    Then tasks_per_hour is 0.0

  Scenario: The rate survives an orchestrator restart
    Given "host-a" completed 2 tasks within the window
    When the orchestrator restarts
    Then tasks_per_hour for "host-a" still counts those 2 tasks
