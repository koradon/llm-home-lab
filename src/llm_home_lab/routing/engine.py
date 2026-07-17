from collections.abc import Sequence

from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.routing.models import (
    NoAvailableBackendError,
    RoutingCandidate,
    RoutingContext,
    RoutingDecision,
    RoutingPolicy,
)


def _estimate_token_budget(request: ChatCompletionRequest) -> int:
    total_chars = sum(len(message.content) for message in request.messages)
    return -(-total_chars // 4)


def _score_candidates(
    policy: RoutingPolicy, eligible: Sequence[RoutingCandidate], context: RoutingContext
) -> tuple[dict[str, float], dict[str, list[str]]]:
    scores: dict[str, float] = {}
    matched_rules: dict[str, list[str]] = {}
    for candidate in eligible:
        total = 0.0
        matched: list[str] = []
        for rule in policy.rules:
            if rule.applies_to(context.task_type):
                total += rule.score_fn(candidate, context)
                matched.append(rule.name)
        scores[candidate.backend.backend_id] = total
        matched_rules[candidate.backend.backend_id] = matched
    return scores, matched_rules


class RoutingEngine:
    def __init__(self, policy: RoutingPolicy, sticky_sessions_enabled: bool = True) -> None:
        self._policy = policy
        self._sticky_sessions_enabled = sticky_sessions_enabled
        self._sticky_assignments: dict[str, str] = {}

    def sticky_backend_for(self, session_id: str) -> str | None:
        return self._sticky_assignments.get(session_id)

    def select_backend(
        self,
        request: ChatCompletionRequest,
        candidates: Sequence[RoutingCandidate],
        session_id: str | None = None,
    ) -> RoutingDecision:
        token_budget = _estimate_token_budget(request)
        eligible = [c for c in candidates if c.context_window >= token_budget]

        if not eligible:
            raise NoAvailableBackendError(
                f"no candidate backend has a context window >= {token_budget} tokens"
            )

        context = RoutingContext(task_type=request.task_type, token_budget=token_budget)
        scores, matched_rules = _score_candidates(self._policy, eligible, context)

        sticky_backend_id = (
            self._sticky_assignments.get(session_id)
            if session_id is not None and self._sticky_sessions_enabled
            else None
        )

        if sticky_backend_id is not None and sticky_backend_id in scores:
            winner_backend_id = sticky_backend_id
        else:
            winner = min(
                eligible,
                key=lambda c: (-scores[c.backend.backend_id], c.backend.backend_id),
            )
            winner_backend_id = winner.backend.backend_id
            if (
                session_id is not None
                and self._sticky_sessions_enabled
                and session_id not in self._sticky_assignments
            ):
                self._sticky_assignments[session_id] = winner_backend_id

        return RoutingDecision(
            backend_id=winner_backend_id,
            scores=scores,
            matched_rules=matched_rules,
        )
