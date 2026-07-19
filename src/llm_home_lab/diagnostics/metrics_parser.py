import math
import re

from llm_home_lab.diagnostics.models import ParsedMetrics

_QUEUE_DEPTH_RE = re.compile(r"^llm_home_lab_queue_depth\s+(\S+)$")
_TOKEN_USAGE_RE = re.compile(r'^llm_home_lab_token_usage_total\{host_id="([^"]+)"\}\s+(\S+)$')
_P95_LATENCY_RE = re.compile(r"^llm_home_lab_request_latency_p95_ms\s+(\S+)$")


def parse_metrics_text(body: str) -> ParsedMetrics:
    parsed = ParsedMetrics()
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue

        if match := _QUEUE_DEPTH_RE.match(line):
            parsed.queue_depth = _as_int(match.group(1))
            continue

        if match := _P95_LATENCY_RE.match(line):
            parsed.p95_latency_ms = _as_float(match.group(1))
            continue

        if match := _TOKEN_USAGE_RE.match(line):
            host_id, value = match.group(1), _as_int(match.group(2))
            if value is not None:
                parsed.token_usage_total[host_id] = value

    return parsed


def _as_int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def _as_float(value: str) -> float | None:
    try:
        result = float(value)
    except ValueError:
        return None
    return None if math.isnan(result) else result
