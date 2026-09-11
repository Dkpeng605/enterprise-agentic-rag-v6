"""Deterministic answer/citation verification, one repair, and bounded abstention."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.domain.retrieval import Citation, QueryPlan
from enterprise_rag.services.scope_root import RootContext


class VerificationIssue(StrEnum):
    DUPLICATE_CITATION = "duplicate_citation"
    UNKNOWN_ROOT = "unknown_root"
    INVALID_LEAF = "invalid_leaf"
    QUOTE_NOT_FOUND = "quote_not_found"
    UNCITED_FACT = "uncited_fact"
    UNKNOWN_CITATION = "unknown_citation"
    MISSING_REQUIREMENT = "missing_requirement"
    EVIDENCE_CONFLICT = "evidence_conflict"


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    REPAIRED = "repaired"
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class DraftCitation:
    id: int
    root_id: str
    leaf_ids: tuple[str, ...]
    quote: str

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("citation id must be positive")
        if not self.root_id.startswith("root_"):
            raise ValueError("citation root_id must use the root_ prefix")
        if not self.leaf_ids or any(not item.startswith("leaf_") for item in self.leaf_ids):
            raise ValueError("citation leaf_ids must contain Leaf IDs")
        if len(self.leaf_ids) != len(set(self.leaf_ids)):
            raise ValueError("citation leaf_ids must not contain duplicates")
        require_non_empty(self.quote, "quote")


@dataclass(frozen=True, slots=True)
class DraftParagraph:
    text: str
    citation_ids: tuple[int, ...]
    factual: bool = True

    def __post_init__(self) -> None:
        require_non_empty(self.text, "text")
        if any(value <= 0 for value in self.citation_ids):
            raise ValueError("paragraph citation IDs must be positive")
        if len(self.citation_ids) != len(set(self.citation_ids)):
            raise ValueError("paragraph citation IDs must not contain duplicates")


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    paragraphs: tuple[DraftParagraph, ...]
    citations: tuple[DraftCitation, ...]
    covered_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.paragraphs:
            raise ValueError("answer draft must contain a paragraph")
        if len(self.covered_requirements) != len(set(self.covered_requirements)):
            raise ValueError("covered_requirements must not contain duplicates")
        if any(not value.strip() for value in self.covered_requirements):
            raise ValueError("covered_requirements must not contain blank values")


@dataclass(frozen=True, slots=True)
class RepairRequest:
    plan: QueryPlan
    roots: tuple[RootContext, ...]
    rejected_draft: AnswerDraft
    issues: tuple[VerificationIssue, ...]
    missing_requirements: tuple[str, ...]


class AnswerRepairer(Protocol):
    async def repair(self, request: RepairRequest) -> AnswerDraft: ...


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    status: AnswerStatus
    answer: str
    citations: tuple[Citation, ...]
    missing_requirements: tuple[str, ...]
    issues: tuple[VerificationIssue, ...]
    repair_count: int


@dataclass(frozen=True, slots=True)
class _Verification:
    valid: bool
    issues: tuple[VerificationIssue, ...]
    missing_requirements: tuple[str, ...]


class AnswerVerificationService:
    def __init__(self, repairer: AnswerRepairer, *, max_repairs: int = 1) -> None:
        if not 0 <= max_repairs <= 1:
            raise ValueError("max_repairs must be zero or one")
        self._repairer = repairer
        self._max_repairs = max_repairs

    async def finalize(
        self,
        *,
        plan: QueryPlan,
        roots: Sequence[RootContext],
        draft: AnswerDraft,
        evidence_conflicts: Sequence[str] = (),
    ) -> AnswerOutcome:
        root_tuple = tuple(roots)
        if evidence_conflicts:
            return self._abstain(
                plan,
                (VerificationIssue.EVIDENCE_CONFLICT,),
                plan.requirements,
                0,
            )
        verification = self._verify(plan, root_tuple, draft)
        if verification.valid:
            return self._answer(AnswerStatus.ANSWERED, root_tuple, draft, 0)
        if not root_tuple or self._max_repairs == 0:
            return self._abstain(plan, verification.issues, verification.missing_requirements, 0)
        try:
            repaired = await self._repairer.repair(
                RepairRequest(
                    plan,
                    root_tuple,
                    draft,
                    verification.issues,
                    verification.missing_requirements,
                )
            )
            repaired_verification = self._verify(plan, root_tuple, repaired)
        except Exception:
            return self._abstain(plan, verification.issues, verification.missing_requirements, 1)
        if repaired_verification.valid:
            return self._answer(AnswerStatus.REPAIRED, root_tuple, repaired, 1)
        return self._abstain(
            plan,
            repaired_verification.issues,
            repaired_verification.missing_requirements,
            1,
        )

    @staticmethod
    def _verify(
        plan: QueryPlan, roots: tuple[RootContext, ...], draft: AnswerDraft
    ) -> _Verification:
        issues: list[VerificationIssue] = []
        root_by_id = {root.root_id: root for root in roots}
        citation_ids = [citation.id for citation in draft.citations]
        if len(citation_ids) != len(set(citation_ids)):
            _add_issue(issues, VerificationIssue.DUPLICATE_CITATION)
        valid_citation_ids: set[int] = set()
        for citation in draft.citations:
            root = root_by_id.get(citation.root_id)
            if root is None:
                _add_issue(issues, VerificationIssue.UNKNOWN_ROOT)
                continue
            if not set(citation.leaf_ids).issubset(root.leaf_ids):
                _add_issue(issues, VerificationIssue.INVALID_LEAF)
                continue
            if citation.quote not in root.text:
                _add_issue(issues, VerificationIssue.QUOTE_NOT_FOUND)
                continue
            valid_citation_ids.add(citation.id)
        for paragraph in draft.paragraphs:
            if paragraph.factual and not paragraph.citation_ids:
                _add_issue(issues, VerificationIssue.UNCITED_FACT)
            if any(citation_id not in valid_citation_ids for citation_id in paragraph.citation_ids):
                _add_issue(issues, VerificationIssue.UNKNOWN_CITATION)
        requirements = set(plan.requirements)
        covered = set(draft.covered_requirements)
        if not covered.issubset(requirements):
            _add_issue(issues, VerificationIssue.MISSING_REQUIREMENT)
        missing = tuple(item for item in plan.requirements if item not in covered)
        if missing:
            _add_issue(issues, VerificationIssue.MISSING_REQUIREMENT)
        return _Verification(not issues, tuple(issues), missing)

    @staticmethod
    def _answer(
        status: AnswerStatus,
        roots: tuple[RootContext, ...],
        draft: AnswerDraft,
        repair_count: int,
    ) -> AnswerOutcome:
        root_by_id = {root.root_id: root for root in roots}
        citations = tuple(
            _citation(citation, root_by_id[citation.root_id]) for citation in draft.citations
        )
        return AnswerOutcome(
            status,
            "\n\n".join(paragraph.text for paragraph in draft.paragraphs),
            citations,
            (),
            (),
            repair_count,
        )

    @staticmethod
    def _abstain(
        plan: QueryPlan,
        issues: tuple[VerificationIssue, ...],
        missing: tuple[str, ...],
        repair_count: int,
    ) -> AnswerOutcome:
        missing_values = missing or plan.requirements
        detail = "、".join(missing_values) if missing_values else "可验证证据"
        return AnswerOutcome(
            AnswerStatus.ABSTAINED,
            f"当前证据不足或存在冲突，无法可靠回答。缺少：{detail}。",
            (),
            missing_values,
            issues,
            repair_count,
        )


def _citation(draft: DraftCitation, root: RootContext) -> Citation:
    page_value = root.source_locator.get("page")
    page = page_value if isinstance(page_value, int) and not isinstance(page_value, bool) else None
    section_value = root.source_locator.get("section") or root.source_locator.get("sheet")
    section = section_value if isinstance(section_value, str) and section_value.strip() else None
    return Citation(
        draft.id,
        root.document_id,
        root.root_id,
        draft.leaf_ids,
        root.source_name,
        root.title,
        page,
        section,
        draft.quote,
        root.score,
    )


def _add_issue(issues: list[VerificationIssue], issue: VerificationIssue) -> None:
    if issue not in issues:
        issues.append(issue)
