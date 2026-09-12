from uuid import UUID

import pytest

from enterprise_rag.domain import QueryMode, QueryScope
from enterprise_rag.services import (
    KnowledgeApplication,
    KnowledgeQuery,
    McpApplicationService,
    Principal,
    QueryApiService,
    QueryCommand,
    QueryExecution,
    QueryRunStatus,
)
from enterprise_rag.services.query_api import ProgressSink

QUERY_ID = UUID("01900000-0000-7000-8000-000000002001")
SESSION_ID = UUID("01900000-0000-7000-8000-000000002002")
TENANT_ID = UUID("01900000-0000-7000-8000-000000002003")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000002004")


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[QueryCommand] = []

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        self.commands.append(command)
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "shared answer",
            (),
            {"mode": command.mode.value},
            {"llm_calls": 2, "input_tokens": 20, "output_tokens": 3},
        )


@pytest.mark.anyio
async def test_rest_and_mcp_facades_share_one_query_use_case() -> None:
    runner = RecordingRunner()
    knowledge = KnowledgeApplication(
        QueryApiService(runner), query_id_factory=lambda: QUERY_ID
    )
    mcp = McpApplicationService(knowledge)
    principal = Principal(SESSION_ID, TENANT_ID, ACTOR_ID)
    request = KnowledgeQuery("  shared question  ", QueryMode.DEEP, QueryScope())

    rest_result = await knowledge.execute(principal, request)
    mcp_result = await mcp.query_knowledge_base(principal, request)

    assert rest_result.to_dict() == mcp_result.to_dict()
    assert len(runner.commands) == 2
    assert runner.commands[0] == runner.commands[1]
    assert runner.commands[0].query == "shared question"
    assert runner.commands[0].tenant_id == TENANT_ID
    assert runner.commands[0].session_id == SESSION_ID


def test_application_layer_rejects_invalid_generated_identity() -> None:
    knowledge = KnowledgeApplication(
        QueryApiService(RecordingRunner()),
        query_id_factory=lambda: UUID("00000000-0000-4000-8000-000000000001"),
    )

    with pytest.raises(Exception) as raised:
        knowledge.command(
            Principal(SESSION_ID, TENANT_ID, ACTOR_ID), KnowledgeQuery("question")
        )

    assert "VALIDATION_ERROR" in str(raised.value)
