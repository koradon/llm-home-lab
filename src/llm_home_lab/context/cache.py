from collections import OrderedDict

from llm_home_lab.context.assembler import assemble_context
from llm_home_lab.context.models import AssembledContext
from llm_home_lab.state.models import Session

_CacheKey = tuple[str, tuple[tuple[int, str, str], ...], tuple[int, str] | None, int]


def _cache_key(session: Session, token_budget: int) -> _CacheKey:
    messages_key = tuple((m.seq, m.role, m.content) for m in session.messages)
    summary_key = (
        (session.summary.covers_up_to_seq, session.summary.summary_text)
        if session.summary
        else None
    )
    return (session.session_id, messages_key, summary_key, token_budget)


class ContextCache:
    def __init__(self, max_entries: int = 128) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[_CacheKey, AssembledContext] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.compaction_count = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def assemble(self, session: Session, token_budget: int) -> AssembledContext:
        key = _cache_key(session, token_budget)

        if key in self._entries:
            self.hits += 1
            self._entries.move_to_end(key)
            return self._entries[key]

        self.misses += 1
        result = assemble_context(session, token_budget)
        if result.compacted:
            self.compaction_count += 1

        self._entries[key] = result
        self._entries.move_to_end(key)
        if len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

        return result
