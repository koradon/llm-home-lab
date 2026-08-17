Feature: Capacity summary endpoint
  External consumers can read total online capacity as a single number, without
  re-deriving it from the full node list.

  # Related spec: docs/specs/2026-08-10-capacity-summary-endpoint.md

  Scenario: Mixed online, offline, and unknown hosts sum only the online ones
    Given a host "a" is registered with max_concurrent_requests 4 and status "online"
    And a host "b" is registered with max_concurrent_requests 6 and status "offline"
    And a host "c" is registered with max_concurrent_requests 2 and status "unknown"
    When a client calls GET /v1/capacity
    Then the response has HTTP status 200
    And the response body reports total_max_concurrent_requests 4

  Scenario: No online hosts returns zero, not an error
    Given no registered host has status "online"
    When a client calls GET /v1/capacity
    Then the response has HTTP status 200
    And the response body reports total_max_concurrent_requests 0

  Scenario: An online host with zero capacity contributes nothing
    Given a host "d" is registered with max_concurrent_requests 0 and status "online"
    And a host "e" is registered with max_concurrent_requests 5 and status "online"
    When a client calls GET /v1/capacity
    Then the response has HTTP status 200
    And the response body reports total_max_concurrent_requests 5
