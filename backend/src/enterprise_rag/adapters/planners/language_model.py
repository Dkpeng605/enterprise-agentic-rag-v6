"""Structured query planner backed by the configured language model."""

import json
from collections.abc import Mapping
from typing import cast

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.llm import CompletionRequest, CompletionResult, LanguageModel
from enterprise_rag.ports.planner import PlannerProviderResult, PlannerRequest
from enterprise_rag.ports.provider import ProviderInfo

_SYSTEM_PROMPT = """You are the query-planning component of a retrieval system.
Return exactly one JSON object and no Markdown, commentary, or reasoning.

Your tasks:
1. Rewrite the current query into a self-contained retrieval query. Resolve pronouns and omitted
   subjects only from the supplied conversation history. Never invent facts.
2. Produce 1 to 4 distinct retrieval sub-queries. A simple factual request normally needs one
   precise sub-query. Comparisons, multi-part requests, and multi-hop questions need 2 to 4
   complementary sub-queries. Do not create duplicates merely to increase the count.
3. List the factual requirements that the final answer must cover.
4. Preserve the requested scope exactly. Never add or replace IDs or metadata filters.

Use exactly these top-level fields:
{
  "rewritten_query": "non-empty string",
  "intent": "factual|comparison|procedural|summary",
  "sub_queries": ["1 to 4 unique non-empty strings"],
  "requirements": ["0 to 8 unique non-empty strings"],
  "scope": {
    "collection_ids": [], "document_ids": [], "titles": [], "organizations": [],
    "doc_types": [], "versions": [], "sections": []
  },
  "language": "BCP-47-style language tag such as zh or en"
}
"""


class LanguageModelQueryPlanner:
    """Turn a shared LanguageModel into an untrusted structured Planner Provider."""

    def __init__(self, language_model: LanguageModel, *, max_output_tokens: int = 1_000) -> None:
        if max_output_tokens <= 0:
            raise ValueError("planner max_output_tokens must be positive")
        self._language_model = language_model
        self._max_output_tokens = max_output_tokens

    def info(self) -> ProviderInfo:
        source = self._language_model.info()
        return ProviderInfo(
            kind=source.kind,
            name=f"{source.name}_query_planner",
            version=source.version,
            capabilities=source.capabilities
            | frozenset({"structured-output", "query-rewrite", "query-decomposition"}),
            is_remote=source.is_remote,
            health=source.health,
        )

    async def plan(self, request: PlannerRequest) -> PlannerProviderResult:
        try:
            completion = await self._language_model.complete(
                CompletionRequest(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=json.dumps(
                        {
                            "query": request.query,
                            "history": [
                                {"role": turn.role.value, "content": turn.content}
                                for turn in request.history
                            ],
                            "requested_scope": request.requested_scope.to_dict(),
                            "mode": request.mode.value,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    max_output_tokens=self._max_output_tokens,
                )
            )
        except AppError as error:
            code = (
                ErrorCode.PLANNER_UNAVAILABLE
                if error.code is ErrorCode.LLM_UNAVAILABLE
                else ErrorCode.PLANNER_INVALID_RESPONSE
            )
            raise AppError(code, "The query planner could not produce a plan.") from error
        except Exception as error:
            raise AppError(
                ErrorCode.PLANNER_UNAVAILABLE,
                "The query planner is unavailable.",
            ) from error
        try:
            payload = json.loads(completion.text)
        except (json.JSONDecodeError, TypeError) as error:
            raise AppError(
                ErrorCode.PLANNER_INVALID_RESPONSE,
                "The query planner returned an invalid response.",
                _usage_details(completion),
            ) from error
        if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
            raise AppError(
                ErrorCode.PLANNER_INVALID_RESPONSE,
                "The query planner returned an invalid response.",
                _usage_details(completion),
            )
        return PlannerProviderResult(
            cast(Mapping[str, object], payload),
            completion.input_tokens,
            completion.output_tokens,
            completion.retry_count,
        )

    async def aclose(self) -> None:
        """The composition root owns the shared language-model lifecycle."""


def _usage_details(completion: CompletionResult) -> dict[str, int]:
    return {
        "llm_calls": 1 + completion.retry_count,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
    }
