Feature: Multi-node registry and scheduler
  Model hosts register with the orchestrator and can join or leave without a restart; the
  scheduling queue admits requests when a host has spare capacity, queuing them by priority and
  fairly within a priority tier when every eligible host is full.

  # Related spec: docs/specs/multi-node-registry-and-scheduler.md

  Scenario: Registering a host makes it immediately eligible
    Given no hosts are registered
    When a host registers with its capabilities and capacity
    Then that host appears in the registry's queryable host list
    And that host is included in scheduling candidates for the next request

  Scenario: Re-registering an existing host updates metadata without resetting in-flight count
    Given a host is registered and currently has requests in flight
    When that host registers again with updated capacity
    Then the host's capacity metadata reflects the update
    And the host's in-flight count is unchanged

  Scenario: A host silent past its TTL is dropped on the next sweep
    Given a host's last heartbeat is older than the expiry TTL
    When the registry expires stale hosts as of the current time
    Then that host no longer appears in the registry's host list
    And that host is no longer offered as a scheduling candidate

  Scenario: Explicit deregistration removes a host immediately
    Given a host is registered and has heartbeat recently
    When that host is deregistered
    Then that host no longer appears in the registry's host list

  Scenario: A request queues when every eligible host is at capacity
    Given every host eligible for a request is already at its maximum concurrent requests
    When that request is enqueued and dispatch is attempted
    Then the request remains queued and is not dispatched

  Scenario: Freeing a slot dispatches the highest-priority waiter
    Given two requests at different priorities are queued for the same host and that host is full
    When a slot on that host frees up and dispatch is attempted
    Then the lower-priority-number request is dispatched first

  Scenario: Fairness prevents one session from monopolizing a priority tier
    Given one session has several requests queued at the same priority as a single request from another session
    When slots free up and dispatch is attempted repeatedly
    Then dispatch alternates between the two sessions rather than draining one session's requests first

  Scenario: Node metadata is queryable regardless of scheduling state
    Given hosts are registered with no requests currently queued or in flight
    When the registry's host list is queried
    Then it reflects the registered hosts' current capabilities and capacity
