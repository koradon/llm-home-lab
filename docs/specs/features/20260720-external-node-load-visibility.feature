Feature: External node load visibility
  An optional probe shells out to LM Studio's `lms` CLI per registered host so the operator can
  see load caused by something other than this orchestrator.

  # Related spec: docs/specs/20260720-external-node-load-visibility.md

  Scenario: A host with an active external generation reports non-idle status
    Given a registered host has a model loaded and lms reports a non-idle generation status
    When the external load probe runs for that host
    Then GET /v1/nodes reports available=true with that non-idle status

  Scenario: A host with nothing loaded reports idle, not unavailable
    Given a registered host has no models currently loaded
    When the external load probe runs for that host
    Then GET /v1/nodes reports available=true, status=idle, queued=0

  Scenario: A missing lms binary degrades only this signal
    Given the lms binary is not present on PATH
    When the external load probe runs for any host
    Then GET /v1/nodes reports available=false for that host
    And every other orchestrator endpoint continues to work normally

  Scenario: A probe that exceeds its timeout is treated as unavailable, not an error
    Given lms ps --host hangs longer than the configured probe timeout
    When the external load probe runs for that host
    Then the probe returns available=false without raising
    And the orchestrator's own health/ready response is unaffected

  Scenario: Repeated probes within the cache window do not spawn a new subprocess
    Given a probe for a host succeeded less than cache_ttl seconds ago
    When the external load probe is invoked again for that host within the window
    Then the cached result is returned without spawning lms again

  Scenario: One host's probe failure does not affect another host's result
    Given host A's lms probe fails and host B's lms probe succeeds
    When both hosts are probed
    Then host A reports available=false and host B reports its real status independently

  Scenario: External load is informational only
    Given a host reports a busy external load status
    When routing selects a candidate for a new request
    Then external load status does not exclude that host from candidacy
