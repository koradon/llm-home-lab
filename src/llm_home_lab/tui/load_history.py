DEFAULT_MAX_POINTS = 30


def update_load_history(
    history: dict[str, list[float]],
    current_ratios: dict[str, float],
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, list[float]]:
    updated: dict[str, list[float]] = {}
    for host_id, ratio in current_ratios.items():
        points = [*history.get(host_id, []), ratio]
        updated[host_id] = points[-max_points:]
    return updated
