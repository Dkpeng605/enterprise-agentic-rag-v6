from dataclasses import replace
from uuid import UUID

from enterprise_rag.domain import QueryIntent, QueryMode, QueryPlan, QueryScope
from enterprise_rag.services.scope_root import RootContext
from enterprise_rag.services.semantic_query import (
    _answer_root_limit,
    _effective_plan,
    _requirements_for_queries,
    _select_answer_roots,
)

DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000003001")
VERSION_ID = UUID("01900000-0000-7000-8000-000000003002")


def plan(*, requirement: str, sub_queries: tuple[str, ...]) -> QueryPlan:
    return QueryPlan(
        requirement,
        requirement,
        QueryIntent.FACTUAL,
        sub_queries,
        (requirement,),
        QueryScope(),
        "zh",
        QueryMode.STANDARD,
        use_sub_queries=len(sub_queries) > 1,
    )


def root(index: int, query: str, *, score: float = 0.9) -> RootContext:
    leaf_id = f"leaf_{index:064x}"
    root_id = f"root_{index:064x}"
    return RootContext(
        root_id,
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


def test_decomposed_standard_plan_can_forward_five_roots() -> None:
    assert _answer_root_limit(
        QueryMode.STANDARD,
        plan(requirement="原始问题", sub_queries=("a", "b")),
    ) == 5
    assert _answer_root_limit(
        QueryMode.STANDARD,
        plan(requirement="a", sub_queries=("a",)),
    ) == 3
    assert _answer_root_limit(
        QueryMode.DEEP,
        plan(requirement="a", sub_queries=("a",)),
    ) == 5


def test_answer_root_selection_keeps_ranked_order_for_one_user_requirement() -> None:
    roots = (
        root(1, "a", score=0.99),
        root(2, "b", score=0.80),
        root(3, "c", score=0.70),
        root(4, "d", score=0.95),
    )
    selected = _select_answer_roots(
        plan(
            requirement="原始问题",
            sub_queries=("a", "b", "c", "d"),
        ),
        roots,
        max_roots=3,
    )

    assert tuple(item.root_id for item in selected) == (
        roots[0].root_id,
        roots[1].root_id,
        roots[2].root_id,
    )


def test_answer_root_selection_fills_ranked_roots_without_branch_requirements() -> None:
    roots = (root(1, "a", score=0.99), root(2, "b", score=0.80), root(3, "c", score=0.70))
    selected = _select_answer_roots(
        plan(requirement="原始问题", sub_queries=("a", "b", "c")),
        roots,
        max_roots=2,
    )

    assert tuple(item.root_id for item in selected) == (
        roots[0].root_id,
        roots[1].root_id,
    )


def test_answer_root_selection_does_not_reserve_a_slot_for_each_route() -> None:
    roots = (
        root(1, "a", score=0.99),
        root(2, "b", score=0.90),
        root(3, "c", score=0.80),
        root(4, "noise", score=0.70),
        root(5, "d", score=0.10),
    )

    selected = _select_answer_roots(
        plan(
            requirement="原始问题",
            sub_queries=("a", "b", "c", "d"),
        ),
        roots,
        max_roots=4,
    )

    assert tuple(item.root_id for item in selected) == tuple(
        item.root_id for item in roots[:4]
    )


def test_answer_root_selection_does_not_treat_branch_provenance_as_new_requirements(
) -> None:
    roots = (
        root(1, "a", score=0.99),
        root(2, "b", score=0.80),
        root(3, "c", score=0.70),
        root(4, "d", score=0.60),
        root(5, "a", score=0.50),
    )
    shared_leaf = roots[0].leaf_ids[0]
    roots = (
        replace(roots[0], leaf_matched_queries={shared_leaf: ("a", "b", "c", "d")}),
        *roots[1:],
    )

    selected = _select_answer_roots(
        plan(
            requirement="原始问题",
            sub_queries=("a", "b", "c", "d"),
        ),
        roots,
        max_roots=4,
    )

    assert len(selected) == 4
    assert tuple(item.root_id for item in selected) == tuple(
        item.root_id for item in roots[:4]
    )


def test_subquery_provenance_maps_to_the_single_original_requirement() -> None:
    assert _requirements_for_queries(
        ("原始用户问题",),
        ("检索路径一", "检索路径二", "检索路径三", "检索路径四"),
        ("检索路径三",),
    ) == ("原始用户问题",)
    assert _requirements_for_queries(
        ("原始用户问题",),
        ("检索路径一",),
        (),
    ) == ()
    assert _requirements_for_queries(
        ("原始用户问题",),
        ("检索路径一", "检索路径二"),
        ("不是本次查询的路径",),
    ) == ()


def test_runtime_binds_requirement_to_the_server_received_original_query() -> None:
    provider_plan = QueryPlan(
        "Provider 伪造的问题",
        "改写后的检索问题",
        QueryIntent.FACTUAL,
        ("改写后的检索问题",),
        ("Provider 伪造的问题",),
        QueryScope(),
        "zh",
        QueryMode.STANDARD,
    )

    effective = _effective_plan(provider_plan, original_query="  用户真正的问题  ")

    assert effective.original_query == "用户真正的问题"
    assert effective.requirements == ("用户真正的问题",)
