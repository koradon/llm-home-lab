Feature: Node test lease
  A holder can take one node out of ordinary routing for a real LLM check, and pin traffic to
  it through the orchestrator, without a forgotten hold lasting longer than one short lease.

  # Related spec: docs/specs/20261025-node-test-lease.md

  Scenario: Reserving the last active node is refused and names the roster
    Given two registered nodes and a minimum of 1 active node
    And one of those nodes is offline
    When a holder reserves without naming a node
    Then the reservation is refused
    And the message lists both nodes, who holds each, and that the minimum would be broken

  Scenario: A second reservation of the same node is refused
    Given host-a is already reserved by ada
    When bea reserves host-a
    Then the reservation is refused
    And the message names ada and the time the lease runs until

  Scenario: An unmodified client using only the leased base URL lands on the reserved node
    Given host-a is reserved and host-b is free
    When a completion is posted to the exported base URL with an API key and no lease header
    Then the orchestrator routes it to host-a
    And a streamed completion on that base is served by host-a
    And capacity is readable under that base
    And the same completion without an API key is rejected
    And a completion on the ordinary URL with no lease header does not select host-a

  Scenario: Ordinary traffic skips a leased node and the lease token pins it
    Given host-a and host-b both serve gemma and host-a is reserved
    When a completion for gemma has no lease header
    Then the orchestrator routes it to host-b
    When a completion for gemma carries host-a's lease token
    Then the orchestrator routes it to host-a and not to host-b

  Scenario: A leased request for a model the node does not serve stays on that node
    Given host-a is reserved and does not serve the requested model
    When a completion carries host-a's lease token
    Then the orchestrator rejects the request
    And it does not route the request to another node

  Scenario: An in-flight request finishes after its node is reserved
    Given a completion is in flight on host-a
    When ada reserves host-a before that completion returns
    Then the in-flight completion still finishes on host-a
    And a later completion without the lease does not select host-a

  Scenario: A missed heartbeat frees the node within one lease
    Given a lease whose ttl is 30 seconds
    When no heartbeat arrives before the expiry
    Then the node is no longer held

  Scenario: The hard ceiling stops a heartbeat from extending the hold
    Given a reservation whose ceiling is shorter than two ttls
    When a heartbeat arrives at the ceiling
    Then the lease has ended

  Scenario: The wrapper returns the command status and always releases
    Given the orchestrator grants a lease
    When llm-home-lab-run runs a command that exits 7
    Then the process exits 7
    And the lease is released
    And the child environment contains the orchestrator base URL and the lease token

  Scenario: A refused reservation does not start the command
    When llm-home-lab-run is refused
    Then the process exits 2
    And the command is not started
    And stderr includes the refusal roster

  Scenario: An unreachable orchestrator is a distinct failure
    When llm-home-lab-run cannot contact the orchestrator
    Then the process exits 3
    And the command is not started

  Scenario: The node view shows every lease colour at once
    Given the node list contains an online node, a reserved node, an expiring node, an offline node, and a reserved node that is offline
    When the dashboard refreshes
    Then each state is visible in the Nodes table
    And the exported screenshot contains distinct colours for reserved, expiring, online, and offline
