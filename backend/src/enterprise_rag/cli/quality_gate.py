"""Run the zero-cost deterministic quality gate used by required CI."""

import argparse
import asyncio
import json
from pathlib import Path

from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.domain import EvaluationRunConfig
from enterprise_rag.services import (
    DeterministicEvaluator,
    EvaluationRunner,
    GoldenSetLoader,
    SparseGoldenSubject,
    evaluate_quality_gate,
    load_quality_gate_policy,
    write_evaluation_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    arguments = parser.parse_args()
    passed, summary = asyncio.run(
        _run(
            arguments.manifest,
            arguments.policy,
            arguments.report,
            arguments.commit_sha,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if not passed:
        raise SystemExit(1)


async def _run(
    manifest: Path, policy_path: Path, report_path: Path, commit_sha: str
) -> tuple[bool, dict[str, object]]:
    golden_set = GoldenSetLoader().load(manifest)
    encoder = HashingSparseEncoder()
    try:
        runner = EvaluationRunner(SparseGoldenSubject(encoder), DeterministicEvaluator())
        report = await runner.run(
            golden_set,
            EvaluationRunConfig(
                provider="hashing_lexical",
                model=encoder.info().version,
                prompt_revision="none",
                index_revision=golden_set.revision,
                commit_sha=commit_sha,
                max_cases=30,
                max_llm_calls=0,
                settings={"purpose": "required-ci-quality-gate", "top_k": 10},
            ),
        )
    finally:
        await encoder.aclose()
    write_evaluation_report(report, report_path)
    result = evaluate_quality_gate(report, load_quality_gate_policy(policy_path))
    return result.passed, {
        "passed": result.passed,
        "failures": list(result.failures),
        "run_id": report.run_id,
        "cases": len(report.cases),
        "metrics": dict(report.aggregate_metrics),
        "report": str(report_path),
    }


if __name__ == "__main__":
    main()
