from dataclasses import dataclass

from llm_home_lab.api.models import Message


@dataclass
class AssembledContext:
    messages: list[Message]
    compacted: bool
    dropped_message_count: int


__all__ = ["AssembledContext"]
