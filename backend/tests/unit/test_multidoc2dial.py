"""Offline acceptance tests for the isolated MultiDoc2Dial adapter."""

import hashlib
import json
from pathlib import Path
from typing import TypedDict

import httpx
import pytest

from enterprise_rag.benchmarks import (
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkInterrupted,
    BenchmarkMode,
    BenchmarkSource,
    BenchmarkSourceError,
    MultiDoc2DialAdapter,
    MultiDoc2DialRunner,
    download_benchmark,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/multidoc2dial"


def dataset() -> BenchmarkDataset:
    documents = json.loads((FIXTURES / "documents.json").read_text(encoding="utf-8"))
    dialogues = json.loads((FIXTURES / "dialogues.json").read_text(encoding="utf-8"))
    return MultiDoc2DialAdapter().convert(
        documents,
        dialogues,
        revision="synthetic-v1",
        source_sha256="a" * 64,
    )


class RunnerPaths(TypedDict):
    checkpoint_path: Path
    output_path: Path
    report_path: Path


def paths(tmp_path: Path) -> RunnerPaths:
    return {
        "checkpoint_path": tmp_path / "checkpoint.json",
        "output_path": tmp_path / "converted.json",
        "report_path": tmp_path / "report.json",
    }


@pytest.mark.anyio
async def test_sample_output_is_never_labeled_full(tmp_path: Path) -> None:
    converted = dataset()
    assert len(converted.documents) == 2
    assert len(converted.cases) == 2
    assert converted.cases[1].expected_document_ids == ("doc-b",)

    report = await MultiDoc2DialRunner().run(
        converted,
        mode=BenchmarkMode.SAMPLE,
        max_cases=1,
        commit_sha="abc123",
        **paths(tmp_path),
    )
    output = json.loads((tmp_path / "converted.json").read_text(encoding="utf-8"))

    assert report.mode is BenchmarkMode.SAMPLE
    assert report.is_full_dataset is False
    assert report.processed_cases == 1
    assert report.total_available_cases == 2
    assert output["mode"] == "sample"
    assert output["is_full_dataset"] is False
    assert len(output["cases"]) == 1


@pytest.mark.anyio
async def test_interrupted_sample_resumes_after_durable_checkpoint(tmp_path: Path) -> None:
    converted = dataset()
    first_calls: list[str] = []

    async def first_processor(case: BenchmarkCase) -> None:
        first_calls.append(case.id)

    with pytest.raises(BenchmarkInterrupted):
        await MultiDoc2DialRunner().run(
            converted,
            mode=BenchmarkMode.SAMPLE,
            max_cases=2,
            commit_sha="abc123",
            processor=first_processor,
            interrupt_after=1,
            **paths(tmp_path),
        )

    resumed_calls: list[str] = []

    async def resumed_processor(case: BenchmarkCase) -> None:
        resumed_calls.append(case.id)

    report = await MultiDoc2DialRunner().run(
        converted,
        mode=BenchmarkMode.SAMPLE,
        max_cases=2,
        commit_sha="abc123",
        processor=resumed_processor,
        **paths(tmp_path),
    )

    assert len(first_calls) == 1
    assert len(resumed_calls) == 1
    assert first_calls != resumed_calls
    assert report.resumed_cases == 1
    assert report.processed_cases == 2
    assert report.is_full_dataset is False


@pytest.mark.anyio
async def test_full_mode_rejects_case_limit_and_marks_only_complete_run(tmp_path: Path) -> None:
    converted = dataset()
    with pytest.raises(ValueError, match="does not accept max_cases"):
        await MultiDoc2DialRunner().run(
            converted,
            mode=BenchmarkMode.FULL,
            max_cases=1,
            commit_sha="abc123",
            **paths(tmp_path),
        )

    report = await MultiDoc2DialRunner().run(
        converted,
        mode=BenchmarkMode.FULL,
        max_cases=None,
        commit_sha="abc123",
        **paths(tmp_path),
    )
    assert report.is_full_dataset is True
    assert report.processed_cases == report.total_available_cases == 2


@pytest.mark.anyio
async def test_download_enforces_size_and_checksum_before_publish(tmp_path: Path) -> None:
    content = b"pinned benchmark"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    source = BenchmarkSource(
        "https://doc2dial.github.io/multidoc2dial/file/multidoc2dial.zip",
        hashlib.sha256(content).hexdigest(),
        len(content),
        "synthetic-v1",
    )
    destination = tmp_path / "benchmark.zip"
    async with httpx.AsyncClient(transport=transport) as client:
        await download_benchmark(source, destination, client=client)
    assert destination.read_bytes() == content

    bad_source = BenchmarkSource(source.url, "0" * 64, len(content), source.revision)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BenchmarkSourceError, match="checksum"):
            await download_benchmark(bad_source, destination, client=client)
    assert not destination.with_suffix(".zip.part").exists()

    oversized = BenchmarkSource(
        source.url,
        hashlib.sha256(content).hexdigest(),
        len(content) - 1,
        source.revision,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BenchmarkSourceError, match="size"):
            await download_benchmark(oversized, destination, client=client)
    assert destination.read_bytes() == content
