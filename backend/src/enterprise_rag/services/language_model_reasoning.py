"""Strict LLM adapters for evidence assessment and cited answer authoring."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import QueryPlan
from enterprise_rag.ports.llm import CompletionRequest, CompletionResult, LanguageModel
from enterprise_rag.services.answer_verification import (
    AnswerDraft,
    DraftCitation,
    DraftParagraph,
    RepairRequest,
)
from enterprise_rag.services.deep_recovery import (
    EvidenceAssessment,
    EvidenceDecision,
    EvidenceItem,
)
from enterprise_rag.services.scope_root import RootContext


@dataclass(frozen=True, slots=True)
class ModelUsage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            self.llm_calls + other.llm_calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class AuthoredAnswer:
    draft: AnswerDraft
    llm_calls: int
    input_tokens: int
    output_tokens: int


class LanguageModelEvidenceAssessor:
    """Assess requirement coverage from bounded Root evidence using strict JSON."""

    def __init__(
        self,
        language_model: LanguageModel,
        *,
        max_evidence_chars: int = 12_000,
        max_output_tokens: int = 2_000,
    ) -> None:
        if max_evidence_chars <= 0 or max_output_tokens <= 0:
            raise ValueError("evidence assessor limits must be positive")
        self._language_model = language_model
        self._max_evidence_chars = max_evidence_chars
        self._max_output_tokens = max_output_tokens

    @property
    def provider_name(self) -> str:
        return self._language_model.info().name

    async def assess(
        self,
        requirements: tuple[str, ...],
        evidence: tuple[EvidenceItem, ...],
        score: float,
    ) -> EvidenceAssessment:
        del score
        completion = await _complete_json(
            self._language_model,
            CompletionRequest(
                _ASSESS_SYSTEM_PROMPT,
                json.dumps(
                    {
                        "requirements": list(requirements),
                        "evidence": _bounded_evidence(evidence, self._max_evidence_chars),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                self._max_output_tokens,
                True,
            ),
            "The evidence assessor could not assess the retrieved evidence.",
        )
        payload, result = completion
        covered = _string_tuple(payload.get("covered_requirements"), "covered_requirements")
        missing = _string_tuple(payload.get("missing_requirements"), "missing_requirements")
        covered, missing = _canonical_requirement_partition(
            requirements, covered, missing
        )
        conflicts = _string_tuple(payload.get("conflicts"), "conflicts")
        try:
            decision = EvidenceDecision(_string(payload.get("decision"), "decision"))
        except ValueError as error:
            raise _invalid("The evidence assessor returned an invalid decision.", result) from error
        # A model may still split a natural-language question into clauses even
        # after being told that the server owns one requirement. Preserve the
        # strict boundary: a non-exact partition is interpreted conservatively
        # as the original question being uncovered, never as new obligations.
        if missing and decision is EvidenceDecision.ANSWER:
            decision = EvidenceDecision.ABSTAIN
        if conflicts and decision is EvidenceDecision.ANSWER:
            raise _invalid("The evidence assessor ignored an evidence conflict.", result)
        reason = _string(payload.get("reason"), "reason")
        confidence = max((item.confidence for item in evidence), default=0.0)
        assessed_score = 0.7 * (len(covered) / len(requirements)) + 0.3 * confidence
        usage = _usage(result)
        return EvidenceAssessment(
            assessed_score,
            covered,
            missing,
            conflicts,
            decision,
            reason,
            usage.llm_calls,
            usage.input_tokens,
            usage.output_tokens,
        )


def _canonical_requirement_partition(
    requirements: tuple[str, ...],
    covered: tuple[str, ...],
    missing: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keep assessor output on the server-owned requirement boundary.

    Query planning deliberately exposes only the original user question as an
    answer obligation. LLMs sometimes split a compound sentence into clauses
    in the assessor response anyway. With one requirement, exact coverage is
    the only safe positive signal; any non-exact partition is therefore mapped
    to the complete original question being missing.

    Multi-requirement callers remain supported for older service contracts and
    retain strict exact-set validation.
    """

    if len(requirements) != 1:
        requirement_set = set(requirements)
        if (
            set(covered) | set(missing) != requirement_set
            or set(covered) & set(missing)
        ):
            raise ValueError("The evidence assessor returned an invalid requirement partition.")
        return covered, missing
    requirement = requirements[0]
    if requirement in missing or missing:
        return (), (requirement,)
    if requirement in covered:
        return (requirement,), ()
    return (), (requirement,)


class LanguageModelAnswerAuthor:
    """Generate a machine-checkable answer draft and perform at most one repair."""

    def __init__(self, language_model: LanguageModel, *, max_output_tokens: int = 3_000) -> None:
        if max_output_tokens <= 0:
            raise ValueError("answer author max_output_tokens must be positive")
        self._language_model = language_model
        self._max_output_tokens = max_output_tokens
        self._repair_usage = ModelUsage()
        self._model_requests: list[CompletionRequest] = []

    @property
    def repair_usage(self) -> ModelUsage:
        return self._repair_usage

    @property
    def model_requests(self) -> tuple[CompletionRequest, ...]:
        return tuple(self._model_requests)

    async def draft(
        self, *, plan: QueryPlan, roots: Sequence[RootContext]
    ) -> AuthoredAnswer:
        payload = _answer_payload(plan, roots)
        request = CompletionRequest(
            _ANSWER_SYSTEM_PROMPT,
            payload,
            self._max_output_tokens,
            True,
        )
        try:
            draft, completion = await self._generate(request)
            usage = _usage(completion)
        except AppError as error:
            if error.code is not ErrorCode.LLM_INVALID_RESPONSE:
                raise
            failed_usage = _error_usage(error)
            retry_request = CompletionRequest(
                _ANSWER_SYSTEM_PROMPT
                + "\nYour previous draft failed the JSON schema. Regenerate it once "
                "with the exact types.",
                payload,
                self._max_output_tokens,
                True,
            )
            try:
                draft, completion = await self._generate(retry_request)
            except AppError as retry_error:
                raise _with_usage(
                    retry_error, failed_usage + _error_usage(retry_error)
                ) from retry_error
            usage = failed_usage + _usage(completion)
        return AuthoredAnswer(
            draft,
            usage.llm_calls,
            usage.input_tokens,
            usage.output_tokens,
        )

    async def repair(self, request: RepairRequest) -> AnswerDraft:
        completion_request = CompletionRequest(
            _ANSWER_SYSTEM_PROMPT,
            json.dumps(
                {
                    "task": "repair",
                    "issues": [item.value for item in request.issues],
                    "missing_requirements": list(request.missing_requirements),
                    "rejected_draft": _draft_dict(request.rejected_draft),
                    "answer_context": json.loads(_answer_payload(request.plan, request.roots)),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            self._max_output_tokens,
            True,
        )
        draft, completion = await self._generate(completion_request)
        self._repair_usage = self._repair_usage + _usage(completion)
        return draft

    async def _generate(
        self, request: CompletionRequest
    ) -> tuple[AnswerDraft, CompletionResult]:
        self._model_requests.append(request)
        payload, completion = await _complete_json(
            self._language_model,
            request,
            "The language model could not produce a verifiable answer.",
        )
        try:
            paragraphs_value = payload.get("paragraphs")
            citations_value = payload.get("citations")
            if not isinstance(paragraphs_value, list) or not isinstance(citations_value, list):
                raise ValueError("answer collections must be lists")
            paragraphs = tuple(_paragraph(item) for item in paragraphs_value)
            citations = tuple(_citation(item) for item in citations_value)
            covered = _string_tuple(payload.get("covered_requirements"), "covered_requirements")
            return AnswerDraft(paragraphs, citations, covered), completion
        except (TypeError, ValueError, KeyError) as error:
            raise _invalid(
                "The language model returned an invalid answer draft.", completion
            ) from error


_ASSESS_SYSTEM_PROMPT = """You are the evidence assessor in a Deep RAG graph.
Return exactly one JSON object, without Markdown or extra text. Judge only the supplied evidence.
The requirements list contains exactly one original user-level question, not one requirement per
retrieval sub-query. Copy that requirement string byte-for-byte when placing it in
covered_requirements or missing_requirements. Never split it by conjunction, punctuation, or
sub-question, and never invent clause-level requirement strings. Evidence from any one sub-query
may be sufficient; do not require every sub-query to produce a separate supporting item.
Sub-query labels are retrieval provenance only: they never create, split, or strengthen a
requirement. If any part of the original question is not reliably supported, place the complete
original requirement string in missing_requirements. Partition the original requirement into
exactly one of covered_requirements or missing_requirements.
List concrete contradictions in conflicts. Use decision answer only when all requirements are
covered
and there are no conflicts; use recover when another retrieval could help; otherwise use abstain.
Fields: covered_requirements, missing_requirements, conflicts, decision, reason."""

_ANSWER_SYSTEM_PROMPT = """You are the cited answer author in an enterprise RAG graph.
Return exactly one compact JSON object, without Markdown, extra text, or a chain of thought. Do not
show step-by-step reasoning, analysis, or a plan. Think privately and output only the final object.
Use only supplied Root evidence. The leaf_evidence requirement_hints are retrieval provenance,
not proof; verify every claim against the accompanying source text. The query plan contains one
original user-level requirement. Sub-queries are alternative retrieval routes only: they never
create requirements, and you do not need a separate paragraph or citation for each route. If one
route supplies sufficient reliable evidence for the original requirement, that is enough; do not
reject a grounded answer because another route returned no evidence. For every requirement that
the source supports, write a concise factual paragraph or clearly separated sentence and cite it.
Keep the response small: at most 4 short paragraphs, at most 6 citations, and one short verbatim
quote per citation (preferably under 120 characters). Each paragraph has text, citation_ids, and
factual. Each citation has id, root_id, leaf_ids, and a short quote copied verbatim from that Root.
Every factual paragraph needs at least one valid citation. Do not claim a requirement is covered
unless the answer addresses it. If the evidence does not support a requirement, leave it out of
covered_requirements and state the missing point briefly in one non-factual paragraph. Never invent
IDs or evidence. Do not repeat the evidence or the question.
Use this exact shape and JSON types:
{"paragraphs":[{"text":"answer [1]","citation_ids":[1],"factual":true}],
"citations":[{"id":1,"root_id":"root_...","leaf_ids":["leaf_..."],
"quote":"exact contiguous source text"}],"covered_requirements":["exact requirement"]}.
Citation id and every citation_ids item MUST be a JSON integer, never a string or label."""


async def _complete_json(
    model: LanguageModel,
    request: CompletionRequest,
    message: str,
) -> tuple[Mapping[str, object], CompletionResult]:
    try:
        completion = await model.complete(request)
    except AppError:
        raise
    except Exception as error:
        raise AppError(ErrorCode.LLM_UNAVAILABLE, message) from error
    try:
        payload = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError) as error:
        raise _invalid(message, completion) from error
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise _invalid(message, completion)
    return cast(Mapping[str, object], payload), completion


def _bounded_evidence(items: Sequence[EvidenceItem], limit: int) -> list[dict[str, object]]:
    # The assessor receives a bounded window.  Retrieval/root order is optimized
    # for ranking and can put a useful hit from a later alternative route after a
    # long, low-confidence hit.  Rank only this model-input window by confidence
    # so one reliable route can be assessed even when the other routes are empty
    # or the total evidence exceeds the context budget.  The index tie-breaker
    # keeps the payload deterministic and does not create a per-route quota.
    ranked = sorted(
        enumerate(items),
        key=lambda pair: (-pair[1].confidence, pair[0]),
    )
    remaining = limit
    result: list[dict[str, object]] = []
    for _, item in ranked:
        if remaining <= 0:
            break
        text = item.text[:remaining]
        result.append(
            {
                "leaf_id": item.leaf_id,
                "root_id": item.root_id,
                "confidence": item.confidence,
                "text": text,
            }
        )
        remaining -= len(text)
    return result


def _answer_payload(plan: QueryPlan, roots: Sequence[RootContext]) -> str:
    def requirement_hints(queries: Sequence[str]) -> list[str]:
        del queries
        return list(plan.requirements)

    return json.dumps(
        {
            "task": "draft",
            "query": plan.original_query,
            "rewritten_query": plan.rewritten_query,
            "use_sub_queries": plan.use_sub_queries,
            "sub_queries": list(plan.sub_queries),
            "requirements": list(plan.requirements),
            "language": plan.language,
            "roots": [
                {
                    "root_id": root.root_id,
                    "leaf_ids": list(root.leaf_ids),
                    "title": root.title,
                    "source": root.source_name,
                    "text": root.evidence_text or root.text,
                    "leaf_evidence": [
                        {
                            "leaf_id": leaf_id,
                            "matched_queries": list(
                                root.leaf_matched_queries.get(leaf_id, ())
                            ),
                            "requirement_hints": requirement_hints(
                                root.leaf_matched_queries.get(leaf_id, ())
                            ),
                        }
                        for leaf_id in root.leaf_ids
                    ],
                }
                for root in roots
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _paragraph(value: object) -> DraftParagraph:
    item = _mapping(value, "paragraph")
    ids = _integer_tuple(item.get("citation_ids"), "citation_ids")
    factual = item.get("factual", True)
    if not isinstance(factual, bool):
        raise ValueError("factual must be boolean")
    return DraftParagraph(_string(item.get("text"), "text"), ids, factual)


def _citation(value: object) -> DraftCitation:
    item = _mapping(value, "citation")
    identifier = item.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        raise ValueError("citation id must be an integer")
    return DraftCitation(
        identifier,
        _string(item.get("root_id"), "root_id"),
        _string_tuple(item.get("leaf_ids"), "leaf_ids"),
        _string(item.get("quote"), "quote"),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    result = tuple(_string(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _integer_tuple(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ValueError(f"{name} must be a list of integers")
    return tuple(value)


def _draft_dict(draft: AnswerDraft) -> dict[str, object]:
    return {
        "paragraphs": [
            {
                "text": item.text,
                "citation_ids": list(item.citation_ids),
                "factual": item.factual,
            }
            for item in draft.paragraphs
        ],
        "citations": [
            {
                "id": item.id,
                "root_id": item.root_id,
                "leaf_ids": list(item.leaf_ids),
                "quote": item.quote,
            }
            for item in draft.citations
        ],
        "covered_requirements": list(draft.covered_requirements),
    }


def _usage(completion: CompletionResult) -> ModelUsage:
    return ModelUsage(
        1 + completion.retry_count,
        completion.input_tokens,
        completion.output_tokens,
    )


def _error_usage(error: AppError) -> ModelUsage:
    def value(name: str) -> int:
        item = error.details.get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else 0

    return ModelUsage(value("llm_calls"), value("input_tokens"), value("output_tokens"))


def _with_usage(error: AppError, usage: ModelUsage) -> AppError:
    return AppError(
        error.code,
        error.message,
        {
            "llm_calls": usage.llm_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
    )


def _invalid(message: str, completion: CompletionResult) -> AppError:
    usage = _usage(completion)
    return AppError(
        ErrorCode.LLM_INVALID_RESPONSE,
        message,
        {
            "llm_calls": usage.llm_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
    )
