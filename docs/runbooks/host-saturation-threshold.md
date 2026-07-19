# Runbook: host-saturation-threshold

## Symptom

`GET /v1/alerts` shows `host-saturation-threshold:<host_id>` firing (severity: critical). That
host's `in_flight / max_concurrent_requests` ratio has exceeded `0.9` — it is nearly or fully at
capacity.

## Likely cause

- Genuine traffic increase beyond what this host's `max_concurrent_requests` was sized for.
- A slow or hanging backend response holding slots open longer than usual (check
  `llm_home_lab_request_latency_p95_ms` in `/metrics` for a corresponding latency spike).
- Other hosts are unhealthy or removed, concentrating traffic onto this one (check `GET
  /v1/nodes` for the full picture, not just this host).

## First mitigation

1. Check `GET /v1/nodes` for `in_flight` vs `max_concurrent_requests` across every host — confirm
   whether this is isolated to one host or system-wide saturation.
2. If other hosts have spare capacity but aren't receiving traffic, check their health
   (`GET /health/ready`) — an unhealthy host is silently excluded from routing.
3. If genuinely saturated system-wide, register an additional host (`POST /v1/nodes/register`) or
   raise `max_concurrent_requests` for existing hosts if their hardware has headroom.
4. Requests that can't be admitted immediately queue (`SchedulingQueue`) rather than failing
   outright — check `llm_home_lab_queue_depth` in `/metrics` to see how large the backlog has
   grown.
