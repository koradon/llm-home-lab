# Runbook: p95-latency-threshold

## Symptom

`GET /v1/alerts` shows `p95-latency-threshold` firing (severity: warning). P95 request latency
over the rolling window has exceeded the configured threshold (default `5000` ms).

## Likely cause

- A backend model host is slow to respond (larger model, contended hardware, or a host under
  saturation — see `host-saturation-threshold`).
- Network latency to a remote host.
- A `SchedulingQueue` backlog adding wait time before dispatch (check
  `llm_home_lab_queue_depth` in `/metrics`).

## First mitigation

1. Check `llm_home_lab_host_saturation_ratio` per host in `/metrics` — a saturated host is a
   common cause of rising latency (see that runbook if so).
2. Check whether latency is isolated to one model/host or across the board — an isolated slow
   host may need investigation independent of overall system health.
3. This is a warning, not a critical alert — it does not necessarily indicate a request is
   failing, only that responses are slower than the configured expectation. Adjust
   `threshold_value` in `config/alert_rules.json` if `5000` ms does not match this deployment's
   actual expected latency.
