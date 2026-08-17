import math
import re

from llm_home_lab.diagnostics.models import ParsedMetrics


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


_QUEUE_DEPTH_RE = re.compile(r"^llm_home_lab_queue_depth\s+(\S+)$")
_TOKEN_USAGE_RE = re.compile(r'^llm_home_lab_token_usage_total\{host_id="([^"]+)"\}\s+(\S+)$')
_P95_LATENCY_RE = re.compile(r"^llm_home_lab_request_latency_p95_ms\s+(\S+)$")

# (metric name, ParsedMetrics field, value parser) for the per-host completion metrics —
# all share the same `name{host_id="..."} value` shape, so one loop handles all of them.
_HOST_COMPLETION_METRIC_PATTERNS = [
    (re.compile(rf'^{metric_name}\{{host_id="([^"]+)"\}}\s+(\S+)$'), field_name, parser)
    for metric_name, field_name, parser in [
        ("llm_home_lab_host_completions_total", "host_completions_total", _as_int),
        ("llm_home_lab_host_latency_ms_avg", "host_latency_ms_avg", _as_float),
        ("llm_home_lab_host_prompt_tokens_avg", "host_prompt_tokens_avg", _as_float),
        ("llm_home_lab_host_prompt_tokens_min", "host_prompt_tokens_min", _as_int),
        ("llm_home_lab_host_prompt_tokens_max", "host_prompt_tokens_max", _as_int),
        ("llm_home_lab_host_completion_tokens_avg", "host_completion_tokens_avg", _as_float),
        ("llm_home_lab_host_completion_tokens_min", "host_completion_tokens_min", _as_int),
        ("llm_home_lab_host_completion_tokens_max", "host_completion_tokens_max", _as_int),
    ]
]


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
            continue

        for pattern, field_name, parser in _HOST_COMPLETION_METRIC_PATTERNS:
            if match := pattern.match(line):
                metric_host_id, parsed_value = match.group(1), parser(match.group(2))
                if parsed_value is not None:
                    getattr(parsed, field_name)[metric_host_id] = parsed_value
                break

    return parsed
