from dataclasses import dataclass
from datetime import datetime


@dataclass
class QueueEntry:
    request_id: str
    session_id: str
    priority: int
    at: datetime


__all__ = ["QueueEntry"]
