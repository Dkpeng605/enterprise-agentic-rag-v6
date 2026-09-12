"""Download or convert the isolated MultiDoc2Dial public benchmark."""

import argparse
import asyncio
import json
from pathlib import Path

from enterprise_rag.benchmarks import (
    BenchmarkMode,
    BenchmarkSource,
    MultiDoc2DialRunner,
    download_benchmark,
    load_multidoc2dial_archive,
)

OFFICIAL_URL = "https://doc2dial.github.io/multidoc2dial/file/multidoc2dial.zip"
OFFICIAL_SHA256 = "f0c034c249663d7b3cb08b19cf2cc2c3d101372485be982621d4711931a1ce00"
OFFICIAL_REVISION = "multidoc2dial-v1.0-2022-05-01"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download")
    download.add_argument("--output", type=Path, required=True)
    download.add_argument("--url", default=OFFICIAL_URL)
    download.add_argument("--sha256", default=OFFICIAL_SHA256)
    download.add_argument("--revision", default=OFFICIAL_REVISION)
    download.add_argument("--max-bytes", type=int, default=8_000_000)
    convert = commands.add_parser("convert")
    convert.add_argument("--archive", type=Path, required=True)
    convert.add_argument("--sha256", default=OFFICIAL_SHA256)
    convert.add_argument("--revision", default=OFFICIAL_REVISION)
    convert.add_argument("--mode", choices=[mode.value for mode in BenchmarkMode], required=True)
    convert.add_argument("--max-cases", type=int)
    convert.add_argument("--commit-sha", required=True)
    convert.add_argument("--checkpoint", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "download":
        asyncio.run(_download(arguments))
    else:
        asyncio.run(_convert(arguments))


async def _download(arguments: argparse.Namespace) -> None:
    source = BenchmarkSource(
        arguments.url,
        arguments.sha256,
        arguments.max_bytes,
        arguments.revision,
    )
    path = await download_benchmark(source, arguments.output)
    print(
        json.dumps(
            {"downloaded": str(path), "revision": source.revision, "sha256": source.sha256},
            sort_keys=True,
        )
    )


async def _convert(arguments: argparse.Namespace) -> None:
    dataset = load_multidoc2dial_archive(
        arguments.archive,
        revision=arguments.revision,
        expected_sha256=arguments.sha256,
    )
    report = await MultiDoc2DialRunner().run(
        dataset,
        mode=BenchmarkMode(arguments.mode),
        max_cases=arguments.max_cases,
        commit_sha=arguments.commit_sha,
        checkpoint_path=arguments.checkpoint,
        output_path=arguments.output,
        report_path=arguments.report,
    )
    print(
        json.dumps(
            {
                "mode": report.mode.value,
                "is_full_dataset": report.is_full_dataset,
                "processed_cases": report.processed_cases,
                "total_available_cases": report.total_available_cases,
                "resumed_cases": report.resumed_cases,
                "output_sha256": report.output_sha256,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
