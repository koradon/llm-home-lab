from datetime import UTC, datetime, timedelta

import pytest
from registry_test_helpers import new_registry_db_path

from llm_home_lab.observability.completion_log import CompletionLog

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_a_host_with_no_completions_has_zero_tasks_per_hour():
    log = CompletionLog()

    result = log.tasks_per_hour("host-a", T0)

    assert result == 0.0


def test_a_host_with_no_completions_has_zero_not_a_division_error_per_slot():
    log = CompletionLog()

    result = log.tasks_per_hour_per_slot("host-a", T0, total_slots=8)

    assert result == 0.0


@pytest.mark.parametrize("total_slots", [None, 0])
def test_an_unknown_slot_count_reports_no_per_slot_rate(total_slots):
    log = CompletionLog()
    log.record("host-a", T0)

    result = log.tasks_per_hour_per_slot("host-a", T0, total_slots=total_slots)

    assert result is None


def test_completions_inside_a_one_hour_window_count_as_tasks_per_hour():
    log = CompletionLog()
    for minutes in (1, 10, 59):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(minutes=60))

    assert result == 3.0


def test_a_completion_exactly_one_window_old_is_outside_the_window():
    log = CompletionLog()
    log.record("host-a", T0)

    result = log.count("host-a", T0 + timedelta(hours=1))

    assert result == 0


def test_a_completion_just_inside_the_window_edge_is_counted():
    log = CompletionLog()
    log.record("host-a", T0 + timedelta(seconds=1))

    result = log.count("host-a", T0 + timedelta(hours=1))

    assert result == 1


def test_a_shorter_window_is_scaled_up_to_an_hourly_rate():
    log = CompletionLog(window=timedelta(minutes=15))
    for minutes in (1, 5, 10):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour("host-a", T0 + timedelta(minutes=15))

    assert result == 12.0


def test_tasks_per_hour_per_slot_divides_by_the_slot_count():
    log = CompletionLog()
    for minutes in range(16):
        log.record("host-a", T0 + timedelta(minutes=minutes))

    result = log.tasks_per_hour_per_slot("host-a", T0 + timedelta(minutes=30), total_slots=8)

    assert result == 2.0


def test_hosts_are_counted_independently():
    log = CompletionLog()
    log.record("host-a", T0)
    log.record("host-a", T0)
    log.record("host-b", T0)

    result = (log.count("host-a", T0), log.count("host-b", T0))

    assert result == (2, 1)


def test_a_non_positive_window_is_rejected():
    with pytest.raises(ValueError):
        CompletionLog(window=timedelta(0))


def test_completions_survive_a_restart_when_backed_by_a_database():
    db_path = new_registry_db_path()
    CompletionLog(db_path=db_path).record("host-a", T0)
    CompletionLog(db_path=db_path).record("host-a", T0 + timedelta(minutes=5))

    restarted = CompletionLog(db_path=db_path)

    assert restarted.count("host-a", T0 + timedelta(minutes=10)) == 2


def test_the_database_log_applies_the_same_window_edges():
    log = CompletionLog(db_path=new_registry_db_path())
    log.record("host-a", T0)
    log.record("host-a", T0 + timedelta(seconds=1))

    result = log.count("host-a", T0 + timedelta(hours=1))

    assert result == 1


def test_the_database_log_prunes_rows_older_than_the_window_on_write():
    db_path = new_registry_db_path()
    log = CompletionLog(db_path=db_path)
    log.record("host-a", T0)

    log.record("host-a", T0 + timedelta(hours=2))

    assert log.count("host-a", T0 + timedelta(minutes=30)) == 0
