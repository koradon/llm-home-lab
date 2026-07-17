from llm_home_lab.context.assembler import assemble_context
from llm_home_lab.state.models import Session, StoredMessage, Summary


def _message(seq: int, content: str, role: str = "user") -> StoredMessage:
    return StoredMessage(seq=seq, role=role, content=content, created_at="2026-01-01T00:00:00Z")


def test_a_session_that_fits_under_budget_is_returned_unchanged():
    session = Session(session_id="s1", messages=[_message(1, "hi"), _message(2, "hello")])

    result = assemble_context(session, token_budget=1000)

    assert result.compacted is False
    assert result.dropped_message_count == 0
    assert [m.content for m in result.messages] == ["hi", "hello"]


def test_an_oversized_session_selects_the_most_recent_messages_that_fit():
    session = Session(
        session_id="s1",
        messages=[_message(1, "x" * 40), _message(2, "y" * 40), _message(3, "z" * 40)],
    )

    result = assemble_context(session, token_budget=15)

    assert result.compacted is True
    assert result.dropped_message_count == 2
    assert [m.content for m in result.messages] == ["z" * 40]


def test_the_single_most_recent_message_is_never_dropped_even_if_it_exceeds_the_budget_alone():
    session = Session(session_id="s1", messages=[_message(1, "x" * 400)])

    result = assemble_context(session, token_budget=1)

    assert [m.content for m in result.messages] == ["x" * 400]
    assert result.compacted is False
    assert result.dropped_message_count == 0


def test_the_summary_is_never_dropped_by_compaction():
    session = Session(
        session_id="s1",
        messages=[_message(1, "x" * 40), _message(2, "y" * 40), _message(3, "z" * 40)],
        summary=Summary(
            covers_up_to_seq=0, summary_text="earlier stuff", created_at="2026-01-01T00:00:00Z"
        ),
    )

    result = assemble_context(session, token_budget=15)

    assert result.compacted is True
    assert result.messages[0].role == "system"
    assert "earlier stuff" in result.messages[0].content
