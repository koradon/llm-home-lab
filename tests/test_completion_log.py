from datetime import UTC, datetime, timedelta

import pytest

from llm_home_lab.observability.completion_log import CompletionLog

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _log(window: timedelta = timedelta(hours=1)) -> CompletionLog:
    return CompletionLog(window=window, clock=lambda: T0)


def test_a_host_with_no_completions_has_zero_tasks_per_hour():
    log = _log()

    result = log.tasks_per_hour("host-a", T0)

    assert result == 0.0


def test_a_host_with_no_completions_has_zero_not_a_division_error_per_slot():
    log = _log()

    result = log.tasks_per_hour_per_slot("host-a", T0, total_slots=8)

    assert result == 0.0


@pytest.mark.parametrize("total_slots", [None, 0])
def test_an_unknown_slot_count_reports_no_per_slot_rate(total_slots):
    log = _log()
    log.record("host-a", T0)

    result = log.tasks_per_hour_per_slot("host-a", T0, total_slots=total_slots)

    assert result is None


def test_completions_inside_a_one_hour_window_count_as_tasks_per_hour():
    log = _log()
    for minutes in (1, 10, 59):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(minutes=60))

    assert result == 3.0


def test_a_completion_exactly_one_window_old_is_outside_the_window():
    log = _log()
    log.record("host-a", T0)

    result = log.count("host-a", T0 + timedelta(hours=1))

    assert result == 0


def test_a_completion_just_inside_the_window_edge_is_counted():
    log = _log()
    log.record("host-a", T0 + timedelta(seconds=1))

    result = log.count("host-a", T0 + timedelta(hours=1))

    assert result == 1


def test_a_shorter_window_is_scaled_up_to_an_hourly_rate():
    log = _log(timedelta(minutes=15))
    for minutes in (1, 5, 10):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(minutes=15))

    assert result == 12.0


def test_tasks_per_hour_per_slot_divides_by_the_slot_count():
    log = _log()
    for minutes in range(1, 17):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour_per_slot("host-a", T0 + timedelta(minutes=60), total_slots=8)

    assert result == 2.0


def test_hosts_are_counted_independently():
    log = _log()
    log.record("host-a", T0)
    log.record("host-a", T0)
    log.record("host-b", T0)

    result = (log.count("host-a", T0), log.count("host-b", T0))

    assert result == (2, 1)


def test_a_non_positive_window_is_rejected():
    with pytest.raises(ValueError):
        CompletionLog(window=timedelta(0))


def test_an_early_window_divides_by_the_time_observed_so_far():
    log = _log()
    for minutes in (1, 3, 5, 7, 9):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(minutes=10))

    assert result == 30.0


def test_a_full_window_divides_by_the_window():
    log = _log()
    for minutes in (1, 30, 59):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(hours=1))

    assert result == 3.0


def test_beyond_the_window_the_divisor_stays_the_window():
    log = _log()
    for minutes in (130, 150):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(hours=3))

    assert result == 2.0


def test_a_tiny_observed_time_is_floored_at_sixty_seconds():
    log = _log()
    log.record("host-a", T0 + timedelta(seconds=5))

    result = log.tasks_per_hour("host-a", T0 + timedelta(seconds=5))

    assert result == 60.0


def test_no_completions_right_after_start_reads_zero():
    log = _log()

    result = log.tasks_per_hour("host-a", T0)

    assert result == 0.0


def test_the_per_slot_rate_uses_the_observed_time_early_on():
    log = _log()
    for minutes in (1, 2, 3, 4):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour_per_slot("host-a", T0 + timedelta(minutes=5), total_slots=4)

    assert result == 12.0
