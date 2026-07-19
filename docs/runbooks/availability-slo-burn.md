# Runbook: availability-slo-burn

## Symptom

`GET /v1/alerts` shows `availability-slo-burn` firing (severity: critical). Availability over the
rolling window has dropped below the configured SLO target (default `0.99`).

## Likely cause

- A registered host is unreachable or erroring (check `GET /v1/nodes` and `GET /health/ready` for
  per-host health).
- A backend is returning 5xx responses under load (check the `llm_home_lab.access` log for
  `status=5xx` lines around the time the alert fired).
- Every model-capable host for some model is unhealthy or over its memory budget, causing requests
  to fail with `503`.

## First mitigation

1. Check `GET /health/ready` and `GET /v1/nodes` to identify which host(s) are unhealthy.
2. If a host is down, restart or fix it — the registry auto-recovers a host once its health probes
   pass `HealthMonitor`'s recovery threshold, no manual re-registration needed.
3. If no host is actually down, check for a capacity problem instead (see
   `host-saturation-threshold`'s runbook) — availability failures can also be capacity-driven
   (`no_available_backend`/`model_capacity_exceeded` responses count as failures here too).
