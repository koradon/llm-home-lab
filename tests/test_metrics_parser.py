from llm_home_lab.diagnostics.metrics_parser import parse_metrics_text

SAMPLE_SCRAPE = (
    "# HELP llm_home_lab_availability_ratio Fraction of requests in the rolling window "
    "that did not return a 5xx status.\n"
    "# TYPE llm_home_lab_availability_ratio gauge\n"
    "llm_home_lab_availability_ratio 1.0\n"
    "# HELP llm_home_lab_queue_depth Number of requests currently queued awaiting a "
    "free host slot.\n"
    "# TYPE llm_home_lab_queue_depth gauge\n"
    "llm_home_lab_queue_depth 3\n"
    "# HELP llm_home_lab_token_usage_total Cumulative prompt+completion tokens "
    "served, per host.\n"
    "# TYPE llm_home_lab_token_usage_total counter\n"
    'llm_home_lab_token_usage_total{host_id="host-a"} 150\n'
    'llm_home_lab_token_usage_total{host_id="host-b"} 42\n'
)


def test_parses_queue_depth_and_token_usage_from_a_known_good_scrape():
    parsed = parse_metrics_text(SAMPLE_SCRAPE)

    assert parsed.queue_depth == 3
    assert parsed.token_usage_total == {"host-a": 150, "host-b": 42}


def test_missing_queue_depth_line_leaves_field_unset_rather_than_raising():
    body = (
        "# HELP llm_home_lab_availability_ratio ...\n"
        "# TYPE llm_home_lab_availability_ratio gauge\n"
        "llm_home_lab_availability_ratio 1.0\n"
    )

    parsed = parse_metrics_text(body)

    assert parsed.queue_depth is None
    assert parsed.token_usage_total == {}


def test_scrape_with_no_token_usage_lines_leaves_dict_empty():
    body = "llm_home_lab_queue_depth 0\n"

    parsed = parse_metrics_text(body)

    assert parsed.queue_depth == 0
    assert parsed.token_usage_total == {}


def test_parses_p95_latency():
    parsed = parse_metrics_text(SAMPLE_SCRAPE)

    assert parsed.p95_latency_ms is None  # not present in SAMPLE_SCRAPE

    body = SAMPLE_SCRAPE + "llm_home_lab_request_latency_p95_ms 123.5\n"
    parsed = parse_metrics_text(body)

    assert parsed.p95_latency_ms == 123.5


def test_unparseable_metric_value_does_not_raise():
    body = (
        'llm_home_lab_queue_depth NaN\nllm_home_lab_token_usage_total{host_id="host-a"} bogus\n'
        "llm_home_lab_request_latency_p95_ms NaN\n"
    )

    parsed = parse_metrics_text(body)

    assert parsed.queue_depth is None
    assert parsed.token_usage_total == {}
    assert parsed.p95_latency_ms is None
