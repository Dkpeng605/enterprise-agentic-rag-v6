from collections.abc import Sequence
from uuid import UUID

import pytest

from enterprise_rag.domain import QueryScope, RetrievalHit
from enterprise_rag.ports import (
    ResolvedQueryScope,
    ScopeAuthorization,
    StoredLeafEvidence,
    StoredRootEvidence,
)
from enterprise_rag.services import ScopeRootService

TENANT_ID = UUID("01900000-0000-7000-8000-000000001401")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000001402")
VERSION_ID = UUID("01900000-0000-7000-8000-000000001403")


def hit(letter: str, root: str, *, selected: bool = False, score: float = 0.2) -> RetrievalHit:
    return RetrievalHit(
        f"leaf_{letter * 64}",
        f"root_{root * 64}",
        1,
        None,
        score,
        score + 1 if selected else None,
        selected,
    )


class FakeContextRepository:
    def __init__(
        self,
        leaves: Sequence[StoredLeafEvidence] = (),
        roots: Sequence[StoredRootEvidence] = (),
    ) -> None:
        self.leaves = tuple(leaves)
        self.roots = tuple(roots)
        self.scope = ResolvedQueryScope(TENANT_ID, (DOCUMENT_ID,))
        self.requested_leaf_ids: tuple[str, ...] = ()
        self.requested_root_ids: tuple[str, ...] = ()

    async def resolve_scope(
        self, authorization: ScopeAuthorization, requested: QueryScope
    ) -> ResolvedQueryScope:
        assert authorization.tenant_id == TENANT_ID
        assert requested == QueryScope()
        return self.scope

    async def load_leaves(
        self, scope: ResolvedQueryScope, leaf_ids: Sequence[str]
    ) -> tuple[StoredLeafEvidence, ...]:
        assert scope == self.scope
        self.requested_leaf_ids = tuple(leaf_ids)
        return self.leaves

    async def load_roots(
        self, scope: ResolvedQueryScope, root_ids: Sequence[str]
    ) -> tuple[StoredRootEvidence, ...]:
        assert scope == self.scope
        self.requested_root_ids = tuple(root_ids)
        return self.roots


@pytest.mark.anyio
async def test_prepare_candidates_keeps_rrf_order_and_rejects_missing_or_mismatched() -> None:
    a = hit("a", "1")
    b = hit("b", "2")
    c = hit("c", "3")
    repository = FakeContextRepository(
        (
            StoredLeafEvidence(a.leaf_id, a.root_id, DOCUMENT_ID, VERSION_ID, "A evidence"),
            StoredLeafEvidence(b.leaf_id, c.root_id, DOCUMENT_ID, VERSION_ID, "bad mapping"),
        )
    )
    service = ScopeRootService(repository)

    result = await service.prepare_candidates(
        ScopeAuthorization(TENANT_ID, True), QueryScope(), (a, b, c)
    )

    assert [item.hit.leaf_id for item in result.items] == [a.leaf_id]
    assert result.items[0].retrieval_text == "A evidence"
    assert result.rejected_count == 2
    assert repository.requested_leaf_ids == (a.leaf_id, b.leaf_id, c.leaf_id)


@pytest.mark.anyio
async def test_recover_merges_leaf_ids_preserves_root_order_and_enforces_char_budget() -> None:
    a = hit("a", "1", selected=True, score=0.2)
    b = hit("b", "1", selected=True, score=0.3)
    c = hit("c", "2", selected=True, score=0.1)
    repository = FakeContextRepository(
        roots=(
            StoredRootEvidence(
                a.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "policy.txt",
                "Policy",
                "Acme",
                "text/plain",
                "123456",
                {"section": "one"},
            ),
            StoredRootEvidence(
                c.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "policy.txt",
                "Policy",
                "Acme",
                "text/plain",
                "abcdef",
                {"section": "two"},
            ),
        )
    )

    result = await ScopeRootService(repository, max_parent_chars=9).recover(
        repository.scope, (a, b, c)
    )

    assert [root.root_id for root in result.roots] == [a.root_id, c.root_id]
    assert result.roots[0].leaf_ids == (a.leaf_id, b.leaf_id)
    assert result.roots[0].score == b.rerank_score
    # Root text remains complete for deterministic citation verification even when the
    # bounded recovery budget reports the Root as truncated.
    assert result.roots[1].text == "abcdef"
    assert result.roots[1].truncated is True
    assert result.used_chars == 9
    assert result.truncated_count == 1
    assert repository.requested_root_ids == (a.root_id, c.root_id)


@pytest.mark.anyio
async def test_recover_exposes_only_selected_leaf_text_as_model_evidence() -> None:
    selected = hit("a", "1", selected=True, score=0.2)
    repository = FakeContextRepository(
        leaves=(
            StoredLeafEvidence(
                selected.leaf_id,
                selected.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "retrieval-only text",
                "exact Leaf source text",
            ),
        ),
        roots=(
            StoredRootEvidence(
                selected.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "policy.txt",
                "Policy",
                "Acme",
                "text/plain",
                "full Root source text",
            ),
        ),
    )

    result = await ScopeRootService(repository).recover(repository.scope, (selected,))

    assert result.roots[0].text == "full Root source text"
    assert result.roots[0].evidence_text == "exact Leaf source text"


@pytest.mark.anyio
async def test_recover_budgets_model_evidence_not_the_complete_root() -> None:
    first = hit("a", "1", selected=True)
    second = hit("b", "2", selected=True)
    repository = FakeContextRepository(
        leaves=(
            StoredLeafEvidence(
                first.leaf_id,
                first.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "first retrieval text",
                "short evidence A",
            ),
            StoredLeafEvidence(
                second.leaf_id,
                second.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "second retrieval text",
                "short evidence B",
            ),
        ),
        roots=(
            StoredRootEvidence(
                first.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "large.txt",
                "Large",
                "Acme",
                "text/plain",
                "x" * 100,
            ),
            StoredRootEvidence(
                second.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "small.txt",
                "Small",
                "Acme",
                "text/plain",
                "y" * 100,
            ),
        ),
    )

    result = await ScopeRootService(repository, max_parent_chars=32).recover(
        repository.scope, (first, second)
    )

    assert [root.root_id for root in result.roots] == [first.root_id, second.root_id]
    assert [root.evidence_text for root in result.roots] == [
        "short evidence A",
        "short evidence B",
    ]
    assert [root.text for root in result.roots] == ["x" * 100, "y" * 100]
    assert result.used_chars == len("short evidence A") + len("short evidence B")
    assert result.truncated_count == 0


@pytest.mark.anyio
async def test_recover_keeps_late_quote_source_when_context_budget_truncates() -> None:
    selected = hit("a", "1", selected=True)
    repository = FakeContextRepository(
        roots=(
            StoredRootEvidence(
                selected.root_id,
                DOCUMENT_ID,
                VERSION_ID,
                "policy.txt",
                "Policy",
                "Acme",
                "text/plain",
                "0123456789",
            ),
        )
    )

    result = await ScopeRootService(repository, max_parent_chars=5).recover(
        repository.scope, (selected,)
    )

    assert result.roots[0].text == "0123456789"
    assert result.roots[0].truncated is True
    assert result.used_chars == 5


@pytest.mark.anyio
async def test_missing_or_stale_root_is_rejected_and_unselected_hit_is_invalid() -> None:
    selected = hit("a", "1", selected=True)
    repository = FakeContextRepository()
    service = ScopeRootService(repository)

    result = await service.recover(repository.scope, (selected,))
    assert result.roots == ()
    assert result.rejected_count == 1

    with pytest.raises(ValueError, match="selected"):
        await service.recover(repository.scope, (hit("b", "2"),))
