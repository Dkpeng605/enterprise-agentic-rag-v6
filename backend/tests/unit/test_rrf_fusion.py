from enterprise_rag.ports import VectorHit
from enterprise_rag.services import (
    DualSearchResult,
    ReciprocalRankFusion,
    SearchBranchResult,
    SearchDiagnostic,
    SearchMethod,
)


def vector_hit(letter: str, root: str, score: float = 99.0) -> VectorHit:
    return VectorHit(f"leaf_{letter * 64}", f"root_{root * 64}", score, {})


def branch(method: SearchMethod, hits: list[VectorHit]) -> SearchBranchResult:
    return SearchBranchResult(
        method,
        tuple(hits),
        SearchDiagnostic(method, 40, len(hits), 0, 0),
    )


def dual(query: str, dense: list[VectorHit], sparse: list[VectorHit]) -> DualSearchResult:
    return DualSearchResult(
        query,
        branch(SearchMethod.DENSE, dense),
        branch(SearchMethod.SPARSE, sparse),
    )


def test_hand_calculated_multi_query_rrf_ignores_raw_scores_and_deduplicates() -> None:
    a = vector_hit("a", "1", -500.0)
    b = vector_hit("b", "2", 9999.0)
    c = vector_hit("c", "3", 5.0)
    d = vector_hit("d", "4", -1.0)
    results = (
        dual("q1", [a, b, c], [b, d, a]),
        dual("q2", [d, a], []),
    )

    fused = ReciprocalRankFusion(rrf_k=60, top_k=10).fuse(results)

    assert [hit.leaf_id for hit in fused.hits] == [a.leaf_id, b.leaf_id, d.leaf_id, c.leaf_id]
    expected_a = 1 / 61 + 1 / 63 + 1 / 62
    expected_b = 1 / 62 + 1 / 61
    expected_d = 1 / 62 + 1 / 61
    assert fused.hits[0].fused_score == expected_a
    assert fused.hits[1].fused_score == expected_b
    assert fused.hits[2].fused_score == expected_d
    assert fused.hits[0].dense_rank == 1
    assert fused.hits[0].sparse_rank == 3
    assert fused.diagnostic.ranked_list_count == 4
    assert fused.diagnostic.input_hit_count == 8
    assert fused.diagnostic.unique_leaf_count == 4


def test_equal_scores_use_leaf_id_tie_break_independent_of_input_order() -> None:
    a = vector_hit("a", "1")
    b = vector_hit("b", "2")
    fusion = ReciprocalRankFusion(rrf_k=10)

    first = fusion.fuse((dual("q", [b], [a]),))
    second = fusion.fuse((dual("q", [a], [b]),))

    assert [item.leaf_id for item in first.hits] == [a.leaf_id, b.leaf_id]
    assert [item.leaf_id for item in second.hits] == [a.leaf_id, b.leaf_id]


def test_root_quota_and_global_top_k_apply_after_fusion_order() -> None:
    a = vector_hit("a", "1")
    b = vector_hit("b", "1")
    c = vector_hit("c", "1")
    d = vector_hit("d", "2")
    e = vector_hit("e", "3")
    result = dual("quota", [a, b, c, d, e], [a, b, c, d, e])

    fused = ReciprocalRankFusion(rrf_k=60, top_k=3, max_leaves_per_root=2).fuse((result,))

    assert [item.leaf_id for item in fused.hits] == [a.leaf_id, b.leaf_id, d.leaf_id]
    assert fused.diagnostic.root_quota_dropped == 1
    assert fused.diagnostic.top_k_dropped == 1


def test_empty_input_is_an_explicit_empty_fusion() -> None:
    fused = ReciprocalRankFusion().fuse(())
    assert fused.hits == ()
    assert fused.diagnostic.ranked_list_count == 0
    assert fused.diagnostic.unique_leaf_count == 0


def test_conflicting_root_identity_is_rejected() -> None:
    a1 = vector_hit("a", "1")
    a2 = vector_hit("a", "2")

    try:
        ReciprocalRankFusion().fuse((dual("conflict", [a1], [a2]),))
    except ValueError as error:
        assert "conflicting root" in str(error)
    else:
        raise AssertionError("conflicting root IDs must fail")
