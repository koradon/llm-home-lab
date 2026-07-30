Feature: Per-alias completion metrics
  Attribute completion token usage to the specific LM Studio instance a model_aliases round-robin resolved to, not just to the host.

  # Related spec: docs/specs/20260730-per-alias-completion-metrics.md

  Scenario: A node with no model_aliases configured is unaffected
    Given a registered node with no model_aliases entry for the requested model
    When a completion request is served
    Then metrics are recorded exactly as before, with no alias label

  Scenario: A request resolved through model_aliases is attributed to the chosen alias
    Given a node configured with model_aliases mapping "my-model" to ["my-model-a", "my-model-b"]
    When a completion request for "my-model" is served and resolves to "my-model-b"
    Then the alias-keyed metric for (host_id, "my-model-b") increases
    And the existing host-level token_usage_total metric for that host also increases

  Scenario: Per-alias metrics are exposed and parseable
    Given at least one completion has been recorded against a resolved alias
    When the Prometheus metrics endpoint is scraped
    Then a metric line labeled with both host_id and alias is present
    And diagnostics/metrics_parser.py parses that line into the per-alias totals
