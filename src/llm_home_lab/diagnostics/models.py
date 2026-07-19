from dataclasses import dataclass, field


@dataclass
class ParsedMetrics:
    queue_depth: int | None = None
    p95_latency_ms: float | None = None
    token_usage_total: dict[str, int] = field(default_factory=dict)
