from datetime import datetime


def compute_token_rates(
    previous: dict[str, int],
    previous_at: datetime | None,
    current: dict[str, int],
    now: datetime,
) -> dict[str, float | None]:
    if previous_at is None:
        return dict.fromkeys(current)

    elapsed = (now - previous_at).total_seconds()
    if elapsed <= 0:
        return dict.fromkeys(current)

    rates: dict[str, float | None] = {}
    for host_id, total in current.items():
        prev = previous.get(host_id)
        rates[host_id] = None if prev is None or total < prev else (total - prev) / elapsed
    return rates
