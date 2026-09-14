from datetime import UTC, datetime, timedelta
from uuid import UUID

from enterprise_rag.ports import StoredSpan, TraceDetail, TraceSummary
from enterprise_rag.services.query_trace import project_query_trace

NOW = datetime(2026, 9, 14, 1, 2, 3, tzinfo=UTC)
TRACE_ID = "a" * 32
QUERY_ID = UUID("01900000-0000-7000-8000-000000009101")


def span(
    identifier: int,
    name: str,
    attributes: dict[str, object],
    *,
    events: tuple[dict[str, object], ...] = (),
) -> StoredSpan:
    return StoredSpan(
        TRACE_ID,
        f"{identifier:016x}",
        None,
        name,
        NOW + timedelta(milliseconds=identifier),
        NOW + timedelta(milliseconds=identifier + 1),
        "UNSET",
        attributes,
        events,
    )


def test_projects_query_plan_branches_stage_metrics_and_best_cross_branch_rank() -> None:
    spans = (
        span(
            1,
            "rag.query_planning",
            {
                "provider.name": "deterministic",
                "rag.degraded": False,
                "rag.plan.original": "如何部署；同时如何回滚",
                "rag.plan.rewritten": "如何部署；同时如何回滚",
                "rag.plan.intent": "procedural",
                "rag.plan.language": "zh",
                "rag.plan.sub_queries": ["如何部署", "如何回滚"],
                "rag.plan.sub_query_count": 2,
                "rag.llm_calls": 1,
                "rag.input_tokens": 120,
                "rag.output_tokens": 80,
            },
        ),
        span(
            2,
            "rag.retrieval.branch",
            {
                "rag.branch.index": 0,
                "rag.branch.query": "如何部署",
                "rag.branch.dense_requested": 40,
                "rag.branch.dense_returned": 8,
                "rag.branch.sparse_requested": 40,
                "rag.branch.sparse_returned": 6,
                "rag.branch.overlap_count": 3,
                "rag.branch.unique_count": 11,
            },
            events=(
                {
                    "name": "rag.retrieval.candidate",
                    "attributes": {
                        "rag.method": "dense",
                        "rag.rank": 3,
                        "rag.leaf_id": "leaf_01",
                        "rag.root_id": "root_01",
                        "rag.score": 0.7,
                    },
                },
            ),
        ),
        span(
            3,
            "rag.retrieval.branch",
            {
                "rag.branch.index": 1,
                "rag.branch.query": "如何回滚",
                "rag.branch.dense_requested": 40,
                "rag.branch.dense_returned": 7,
                "rag.branch.sparse_requested": 40,
                "rag.branch.sparse_returned": 5,
                "rag.branch.overlap_count": 2,
                "rag.branch.unique_count": 10,
            },
            events=(
                {
                    "name": "rag.retrieval.candidate",
                    "attributes": {
                        "rag.method": "dense",
                        "rag.rank": 1,
                        "rag.leaf_id": "leaf_01",
                        "rag.root_id": "root_01",
                        "rag.score": 0.9,
                    },
                },
            ),
        ),
        span(
            4,
            "rag.rrf_fusion",
            {
                "rag.fusion.ranked_list_count": 4,
                "rag.fusion.input_hit_count": 26,
                "rag.fusion.unique_leaf_count": 18,
                "rag.fusion.root_quota_dropped": 2,
                "rag.fusion.top_k_dropped": 1,
                "rag.candidate_count": 15,
            },
        ),
        span(
            5,
            "rag.auth_and_scope",
            {"rag.input_count": 15, "rag.output_count": 12, "rag.rejected_count": 3},
        ),
        span(
            6,
            "rag.rerank",
            {"rag.input_count": 12, "rag.candidate_count": 10, "rag.output_count": 5},
        ),
        span(
            7,
            "rag.root_restore",
            {
                "rag.input_count": 5,
                "rag.output_count": 3,
                "rag.rejected_count": 2,
                "rag.truncated_count": 1,
                "rag.used_chars": 4096,
            },
        ),
        span(
            8,
            "rag.answer_generation",
            {
                "rag.llm_calls": 1,
                "rag.input_tokens": 512,
                "rag.output_tokens": 96,
                "rag.citation_count": 3,
            },
        ),
    )
    summary = TraceSummary(
        TRACE_ID,
        "query",
        QUERY_ID,
        "standard",
        "answered",
        NOW,
        NOW + timedelta(milliseconds=20),
        20.0,
        len(spans),
        False,
    )

    projected = project_query_trace(
        TraceDetail(summary, "anonymous", None, {"llm_calls": 1}, {}, spans)
    )

    assert projected.plan is not None
    assert projected.plan.provider == "deterministic"
    assert projected.plan.sub_queries == ("如何部署", "如何回滚")
    assert projected.retrieval_branches[0].overlap_count == 3
    assert projected.rankings[0].dense_rank == 1
    assert projected.rankings[0].dense_score == 0.9
    assert [metric.stage for metric in projected.stage_metrics] == [
        "query_planning",
        "rrf_fusion",
        "auth_and_scope",
        "rerank",
        "root_restore",
        "answer_generation",
    ]
    assert projected.stage_metrics[0].output_count == 2
    assert projected.stage_metrics[0].attributes == {
        "llm_calls": 1,
        "input_tokens": 120,
        "output_tokens": 80,
    }
    assert projected.stage_metrics[1].dropped_count == 11
    assert projected.stage_metrics[1].attributes["duplicate_collapsed"] == 8
    assert projected.stage_metrics[4].attributes == {
        "truncated_roots": 1,
        "used_chars": 4096,
    }
