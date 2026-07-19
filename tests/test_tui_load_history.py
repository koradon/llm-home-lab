from llm_home_lab.tui.load_history import update_load_history


def test_first_observation_starts_a_single_point_history():
    history = update_load_history(history={}, current_ratios={"host-a": 0.5})

    assert history == {"host-a": [0.5]}


def test_next_observation_appends_to_existing_history():
    history = update_load_history(history={"host-a": [0.5]}, current_ratios={"host-a": 0.8})

    assert history == {"host-a": [0.5, 0.8]}


def test_history_is_truncated_to_max_points():
    history = update_load_history(
        history={"host-a": [0.1, 0.2, 0.3]}, current_ratios={"host-a": 0.4}, max_points=3
    )

    assert history == {"host-a": [0.2, 0.3, 0.4]}


def test_a_host_no_longer_present_is_dropped_from_the_history():
    history = update_load_history(
        history={"host-a": [0.5], "host-b": [0.1]}, current_ratios={"host-a": 0.6}
    )

    assert history == {"host-a": [0.5, 0.6]}
