from llm_home_lab.api.models import Message
from llm_home_lab.context.models import AssembledContext
from llm_home_lab.state.models import Session


def _estimate_tokens(messages: list[Message]) -> int:
    total_chars = sum(len(m.content) for m in messages)
    return -(-total_chars // 4)


def assemble_context(session: Session, token_budget: int) -> AssembledContext:
    summary_message = (
        Message(
            role="system",
            content=f"Summary of earlier conversation: {session.summary.summary_text}",
        )
        if session.summary
        else None
    )
    raw_messages = [Message(role=m.role, content=m.content) for m in session.messages]
    full_messages = ([summary_message] if summary_message else []) + raw_messages

    if _estimate_tokens(full_messages) <= token_budget:
        return AssembledContext(messages=full_messages, compacted=False, dropped_message_count=0)

    summary_cost = _estimate_tokens([summary_message]) if summary_message else 0
    remaining_budget = token_budget - summary_cost

    kept: list[Message] = []
    for message in reversed(raw_messages):
        cost = _estimate_tokens([message])
        if kept and cost > remaining_budget:
            break
        kept.insert(0, message)
        remaining_budget -= cost

    dropped = len(raw_messages) - len(kept)
    result_messages = ([summary_message] if summary_message else []) + kept
    return AssembledContext(
        messages=result_messages, compacted=dropped > 0, dropped_message_count=dropped
    )
