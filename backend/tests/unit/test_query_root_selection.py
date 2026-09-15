from uuid import UUID

from enterprise_rag.domain import QueryIntent, QueryMode, QueryPlan, QueryScope
from enterprise_rag.services.scope_root import RootContext
from enterprise_rag.services.semantic_query import _select_answer_roots

DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000003101")
VERSION_ID = UUID("01900000-0000-7000-8000-000000003102")


def _plan() -> QueryPlan:
    return QueryPlan(
        "原始问题",
        "改写后的问题",
        QueryIntent.FACTUAL,
        ("路径一", "路径二", "路径三", "路径四"),
        ("原始问题",),
        QueryScope(),
        "zh",
        QueryMode.STANDARD,
        True,
    )


def _root(index: int, query: str, score: float) -> RootContext:
    leaf_id = f"leaf_{index:064x}"
    return RootContext(
        f"root_{index:064x}",
        DOCUMENT_ID,
        VERSION_ID,
        "knowledge.md",
        f"Root {index}",
        None,
        "text/markdown",
        {"section": f"section-{index}"},
        f"evidence {index}",
        (leaf_id,),
        score,
        False,
        leaf_matched_queries={leaf_id: (query,)},
    )


def test_root_selection_uses_ranked_context_without_subquery_coverage_slots() -> None:
    roots = (
        _root(1, "路径一", 0.99),
        _root(2, "路径二", 0.90),
        _root(3, "路径三", 0.80),
        _root(4, "噪声", 0.70),
        _root(5, "路径四", 0.10),
    )

    selected = _select_answer_roots(_plan(), roots, max_roots=4)

    assert tuple(root.root_id for root in selected) == tuple(
        root.root_id for root in roots[:4]
    )
