from dataclasses import dataclass, field


@dataclass
class ParsedMetrics:
    queue_depth: int | None = None
    p95_latency_ms: float | None = None
    token_usage_total: dict[str, int] = field(default_factory=dict)
    host_completions_total: dict[str, int] = field(default_factory=dict)
    host_latency_ms_avg: dict[str, float] = field(default_factory=dict)
    host_prompt_tokens_avg: dict[str, float] = field(default_factory=dict)
    host_prompt_tokens_min: dict[str, int] = field(default_factory=dict)
    host_prompt_tokens_max: dict[str, int] = field(default_factory=dict)
    host_completion_tokens_avg: dict[str, float] = field(default_factory=dict)
    host_completion_tokens_min: dict[str, int] = field(default_factory=dict)
    host_completion_tokens_max: dict[str, int] = field(default_factory=dict)
    host_model_completions_total: dict[tuple[str, str], int] = field(default_factory=dict)
    host_model_latency_ms_avg: dict[tuple[str, str], float] = field(default_factory=dict)
    host_model_prompt_tokens_avg: dict[tuple[str, str], float] = field(default_factory=dict)
    host_model_prompt_tokens_min: dict[tuple[str, str], int] = field(default_factory=dict)
    host_model_prompt_tokens_max: dict[tuple[str, str], int] = field(default_factory=dict)
    host_model_completion_tokens_avg: dict[tuple[str, str], float] = field(default_factory=dict)
    host_model_completion_tokens_min: dict[tuple[str, str], int] = field(default_factory=dict)
    host_model_completion_tokens_max: dict[tuple[str, str], int] = field(default_factory=dict)
