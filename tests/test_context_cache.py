from llm_home_lab.context.cache import ContextCache
from llm_home_lab.state.models import Session, StoredMessage


def _message(seq: int, content: str, role: str = "user") -> StoredMessage:
    return StoredMessage(seq=seq, role=role, content=content, created_at="2026-01-01T00:00:00Z")


def test_repeat_assembly_of_an_unchanged_session_is_a_cache_hit():
    cache = ContextCache()
    session = Session(session_id="s1", messages=[_message(1, "hi")])

    first = cache.assemble(session, token_budget=1000)
    second = cache.assemble(session, token_budget=1000)

    assert first == second
    assert cache.hits == 1
    assert cache.misses == 1


def test_hit_ratio_reflects_hits_and_misses():
    cache = ContextCache()
    session = Session(session_id="s1", messages=[_message(1, "hi")])

    cache.assemble(session, token_budget=1000)
    cache.assemble(session, token_budget=1000)
    cache.assemble(session, token_budget=1000)

    assert cache.hit_ratio == 2 / 3


def test_hit_ratio_is_zero_before_any_calls():
    cache = ContextCache()

    assert cache.hit_ratio == 0.0


def test_a_changed_session_is_a_cache_miss():
    cache = ContextCache()
    session = Session(session_id="s1", messages=[_message(1, "hi")])
    cache.assemble(session, token_budget=1000)

    changed_session = Session(session_id="s1", messages=[_message(1, "hi"), _message(2, "again")])
    cache.assemble(changed_session, token_budget=1000)

    assert cache.hits == 0
    assert cache.misses == 2


def test_compaction_count_only_increments_once_for_a_repeated_compacted_session():
    cache = ContextCache()
    session = Session(
        session_id="s1",
        messages=[_message(1, "x" * 40), _message(2, "y" * 40), _message(3, "z" * 40)],
    )

    cache.assemble(session, token_budget=15)
    cache.assemble(session, token_budget=15)

    assert cache.compaction_count == 1


def test_the_cache_evicts_the_least_recently_used_entry_past_its_size_bound():
    cache = ContextCache(max_entries=2)
    session_a = Session(session_id="a", messages=[_message(1, "a")])
    session_b = Session(session_id="b", messages=[_message(1, "b")])
    session_c = Session(session_id="c", messages=[_message(1, "c")])
    cache.assemble(session_a, token_budget=1000)
    cache.assemble(session_b, token_budget=1000)
    # Touch "a" so "b" becomes the least-recently-used entry.
    cache.assemble(session_a, token_budget=1000)

    # Inserting "c" past the size bound should evict "b", not "a".
    cache.assemble(session_c, token_budget=1000)

    misses_before = cache.misses
    cache.assemble(session_a, token_budget=1000)
    cache.assemble(session_c, token_budget=1000)
    assert cache.misses == misses_before  # both still cached

    cache.assemble(session_b, token_budget=1000)
    assert cache.misses == misses_before + 1  # "b" had to be reassembled
