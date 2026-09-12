"""Run the deterministic fixture evaluation and write a reproducible report."""

import argparse
import asyncio
import json
from pathlib import Path

from enterprise_rag.domain import EvaluationRunConfig
from enterprise_rag.services import (
    DeterministicEvaluator,
    EvaluationRunner,
    GoldenFixtureSubject,
    GoldenSetLoader,
    write_evaluation_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--max-cases", type=int, default=30)
    arguments = parser.parse_args()
    asyncio.run(
        _run(arguments.manifest, arguments.report, arguments.commit_sha, arguments.max_cases)
    )


async def _run(manifest: Path, report_path: Path, commit_sha: str, max_cases: int) -> None:
    golden_set = GoldenSetLoader().load(manifest)
    runner = EvaluationRunner(GoldenFixtureSubject(), DeterministicEvaluator())
    report = await runner.run(
        golden_set,
        EvaluationRunConfig(
            provider="fixture",
            model="deterministic",
            prompt_revision="deterministic-v1",
            index_revision=golden_set.revision,
            commit_sha=commit_sha,
            max_cases=max_cases,
            max_llm_calls=0,
            settings={"purpose": "runner-mechanics-only"},
        ),
    )
    write_evaluation_report(report, report_path)
    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "cases": len(report.cases),
                "report": str(report_path),
                "subject": report.subject_snapshot["name"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
