# The sparkline compresses whatever history it's given down to the terminal's column count, so
# points beyond the widest realistic terminal can never be displayed even after a resize — no
# reason to keep them and grow memory forever. 1000 comfortably covers that (no real terminal
# gets that wide) while still spanning a long observability session at the default 2s poll
# interval, instead of a window measured in minutes.
DEFAULT_MAX_POINTS = 1000


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
