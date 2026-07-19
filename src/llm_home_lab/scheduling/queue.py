from collections import deque
from datetime import datetime

from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.scheduling.models import QueueEntry


def _has_free_capacity(registry: HostRegistry) -> bool:
    return any(host.in_flight < host.capacity.max_concurrent_requests for host in registry.hosts())


class SchedulingQueue:
    def __init__(self) -> None:
        self._tiers: dict[int, dict[str, deque[QueueEntry]]] = {}
        self._rotations: dict[int, deque[str]] = {}

    def enqueue(self, request_id: str, session_id: str, priority: int, at: datetime) -> None:
        tier = self._tiers.setdefault(priority, {})
        if session_id not in tier:
            tier[session_id] = deque()
            self._rotations.setdefault(priority, deque()).append(session_id)
        tier[session_id].append(
            QueueEntry(request_id=request_id, session_id=session_id, priority=priority, at=at)
        )

    def depth(self) -> int:
        return sum(
            len(session_queue) for tier in self._tiers.values() for session_queue in tier.values()
        )

    def cancel(self, request_id: str, session_id: str, priority: int) -> None:
        tier = self._tiers.get(priority)
        if tier is None or session_id not in tier:
            return

        session_queue = tier[session_id]
        remaining = deque(entry for entry in session_queue if entry.request_id != request_id)
        if len(remaining) == len(session_queue):
            return

        if remaining:
            tier[session_id] = remaining
            return

        del tier[session_id]
        rotation = self._rotations.get(priority)
        if rotation is not None and session_id in rotation:
            rotation.remove(session_id)

    def dispatch(self, registry: HostRegistry, at: datetime) -> str | None:
        if not _has_free_capacity(registry):
            return None

        for priority in sorted(self._tiers):
            rotation = self._rotations[priority]
            if not rotation:
                continue

            tier = self._tiers[priority]
            session_id = rotation.popleft()
            session_queue = tier[session_id]
            entry = session_queue.popleft()

            if session_queue:
                rotation.append(session_id)
            else:
                del tier[session_id]

            return entry.request_id

        return None
