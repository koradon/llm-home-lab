# The sparkline compresses whatever history it's given down to the terminal's column count, so
# points beyond the widest realistic terminal can never be displayed even after a resize — no
# reason to keep them and grow memory forever. 1000 comfortably covers that (no real terminal
# gets that wide) while still spanning a long observability session at the default 2s poll
# interval, instead of a window measured in minutes.
DEFAULT_MAX_POINTS = 1000


def update_load_history(
    history: dict[str, list[float | None]],
    current_ratios: dict[str, float],
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, list[float | None]]:
    updated: dict[str, list[float | None]] = {}
    length = max((len(points) for points in history.values()), default=0)
    for host_id in history.keys() | current_ratios.keys():
        points = history.get(host_id, [None] * length)
        points = [*points, current_ratios.get(host_id)][-max_points:]
        if any(point is not None for point in points):
            updated[host_id] = points
    return updated
