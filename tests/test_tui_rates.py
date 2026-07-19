from datetime import UTC, datetime, timedelta

from llm_home_lab.tui.rates import compute_token_rates

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_first_observation_has_no_rate_yet():
    rates = compute_token_rates(previous={}, previous_at=None, current={"host-a": 100}, now=T0)

    assert rates == {"host-a": None}


def test_rate_is_delta_over_elapsed_seconds():
    rates = compute_token_rates(
        previous={"host-a": 100},
        previous_at=T0,
        current={"host-a": 150},
        now=T0 + timedelta(seconds=5),
    )

    assert rates == {"host-a": 10.0}


def test_a_host_not_seen_in_the_previous_snapshot_has_no_rate_yet():
    rates = compute_token_rates(
        previous={"host-a": 100},
        previous_at=T0,
        current={"host-a": 150, "host-b": 30},
        now=T0 + timedelta(seconds=5),
    )

    assert rates == {"host-a": 10.0, "host-b": None}


def test_a_counter_reset_yields_no_rate_instead_of_a_negative_number():
    rates = compute_token_rates(
        previous={"host-a": 500},
        previous_at=T0,
        current={"host-a": 10},
        now=T0 + timedelta(seconds=5),
    )

    assert rates == {"host-a": None}


def test_zero_or_negative_elapsed_time_yields_no_rate():
    rates = compute_token_rates(
        previous={"host-a": 100}, previous_at=T0, current={"host-a": 150}, now=T0
    )

    assert rates == {"host-a": None}
