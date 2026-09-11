"""Structured query-planning Provider contract."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.retrieval import QueryMode, QueryScope
from enterprise_rag.ports.provider import Provider


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: ConversationRole
    content: str

    def __post_init__(self) -> None:
        require_non_empty(self.content, "content")


@dataclass(frozen=True, slots=True)
class PlannerRequest:
    query: str
    history: tuple[ConversationTurn, ...]
    requested_scope: QueryScope
    mode: QueryMode

    def __post_init__(self) -> None:
        require_non_empty(self.query, "query")
        if len(self.history) > 20:
            raise ValueError("planner history must not exceed 20 turns")


class QueryPlannerProvider(Provider, Protocol):
    async def plan(self, request: PlannerRequest) -> Mapping[str, object]: ...
