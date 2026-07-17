Feature: Failover and backend health policy
  The orchestrator tracks each backend's probe history with hysteresis so it stops routing to a
  degraded backend immediately and only resumes once the backend has proven itself recovered.

  # Related spec: docs/specs/failover-and-health-policy.md

  Scenario: A backend with no recorded probes is healthy by default
    Given a backend with no recorded probes
    When the monitor is asked whether that backend is healthy
    Then it reports healthy

  Scenario: Fewer than the failure threshold does not trigger failover
    Given a backend has fewer than the failure threshold of consecutive failed probes recorded
    When the monitor is asked whether that backend is healthy
    Then it still reports healthy

  Scenario: Crossing the failure threshold triggers failover
    Given a backend reaches the failure threshold of consecutive failed probes
    When the monitor is asked whether that backend is healthy
    Then it reports unhealthy
    And a healthy-to-unhealthy failover event is recorded

  Scenario: An unhealthy backend stays excluded for the full cooldown window
    Given a backend became unhealthy and a successful probe is recorded before its cooldown ends
    When the monitor is asked whether that backend is healthy before the cooldown elapses
    Then it still reports unhealthy

  Scenario: Recovery requires consecutive successes after cooldown
    Given a backend's cooldown has elapsed with only one successful probe recorded and the recovery threshold is greater than one
    When the monitor is asked whether that backend is healthy
    Then it still reports unhealthy

  Scenario: A failure during the recovery phase restarts the cooldown
    Given a backend is in its post-cooldown recovery phase and a probe fails before the recovery threshold is reached
    When the monitor is asked whether that backend is healthy
    Then it reports unhealthy
    And a new cooldown window has started from that failed probe

  Scenario: Crossing the recovery threshold ends the failover
    Given a backend accumulates the recovery threshold of consecutive successful probes after its cooldown elapsed
    When the monitor is asked whether that backend is healthy
    Then it reports healthy
    And an unhealthy-to-healthy failover event is recorded

  Scenario: Health score reflects recent probe history independent of exclusion state
    Given a backend is excluded by an active cooldown but has recorded some successful probes
    When the monitor reports that backend's health score
    Then the score reflects the recent probe history rather than the exclusion state

  Scenario: An excluded backend is dropped from routing candidates without breaking sticky fallback
    Given a session has a sticky backend that the health monitor now reports unhealthy
    When routing candidates are filtered by the health monitor before selection
    Then the unhealthy backend is absent from the candidates passed to the routing engine
    And the routing engine falls back to scoring the remaining candidates for that request
