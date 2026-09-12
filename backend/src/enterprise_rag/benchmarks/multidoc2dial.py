"""Isolated MultiDoc2Dial v1 conversion, download, resume, and report support."""

import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import httpx


class BenchmarkMode(StrEnum):
    SAMPLE = "sample"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class BenchmarkSource:
    url: str
    sha256: str
    max_bytes: int
    revision: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "doc2dial.github.io",
            "huggingface.co",
        }:
            raise ValueError("benchmark source must use an approved HTTPS host")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("benchmark source sha256 must be lowercase hexadecimal")
        if self.max_bytes <= 0 or not self.revision.strip():
            raise ValueError("benchmark source size and revision must be valid")


@dataclass(frozen=True, slots=True)
class BenchmarkDocument:
    id: str
    title: str
    domain: str
    text: str
    span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    id: str
    domain: str
    dialogue_id: str
    turn_id: int
    question: str
    history: tuple[str, ...]
    expected_answer: str
    expected_document_ids: tuple[str, ...]
    expected_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    revision: str
    source_sha256: str
    documents: tuple[BenchmarkDocument, ...]
    cases: tuple[BenchmarkCase, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkConversionReport:
    schema_version: str
    dataset: str
    dataset_revision: str
    source_sha256: str
    mode: BenchmarkMode
    is_full_dataset: bool
    total_available_cases: int
    processed_cases: int
    resumed_cases: int
    commit_sha: str
    output_sha256: str


class BenchmarkSourceError(ValueError):
    pass


class BenchmarkInterrupted(RuntimeError):
    pass


async def download_benchmark(
    source: BenchmarkSource,
    destination: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """Stream one approved artifact to disk and publish only after hash verification."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    temporary.unlink(missing_ok=True)
    owns_client = client is None
    active_client = client or httpx.AsyncClient(follow_redirects=True, timeout=60)
    digest = hashlib.sha256()
    size = 0
    try:
        async with active_client.stream("GET", source.url) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > source.max_bytes:
                        raise BenchmarkSourceError("benchmark download exceeds configured size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if digest.hexdigest() != source.sha256:
            raise BenchmarkSourceError("benchmark checksum does not match pinned source")
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            await active_client.aclose()


def load_multidoc2dial_archive(
    archive_path: Path, *, revision: str, expected_sha256: str
) -> BenchmarkDataset:
    actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BenchmarkSourceError("local benchmark archive checksum does not match")
    try:
        with ZipFile(archive_path) as archive:
            documents = json.loads(
                archive.read("multidoc2dial/multidoc2dial_doc.json").decode("utf-8")
            )
            dialogues = json.loads(
                archive.read(
                    "multidoc2dial/multidoc2dial_dial_validation.json"
                ).decode("utf-8")
            )
    except (OSError, BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkSourceError("benchmark archive is malformed") from exc
    return MultiDoc2DialAdapter().convert(
        _mapping(documents, "document payload"),
        _mapping(dialogues, "dialogue payload"),
        revision=revision,
        source_sha256=actual_sha256,
    )


class MultiDoc2DialAdapter:
    """Convert official JSON mappings without importing them into product Domain types."""

    def convert(
        self,
        documents_payload: Mapping[str, Any],
        dialogues_payload: Mapping[str, Any],
        *,
        revision: str,
        source_sha256: str,
    ) -> BenchmarkDataset:
        documents = self._documents(documents_payload)
        cases = self._cases(dialogues_payload, {document.id for document in documents})
        if not documents or not cases:
            raise BenchmarkSourceError("benchmark payload contains no usable documents or cases")
        return BenchmarkDataset(revision, source_sha256, documents, cases)

    @staticmethod
    def _documents(payload: Mapping[str, Any]) -> tuple[BenchmarkDocument, ...]:
        domain_map = _mapping(payload.get("doc_data"), "doc_data")
        documents: list[BenchmarkDocument] = []
        for domain in sorted(domain_map):
            entries = _mapping(domain_map[domain], f"doc_data.{domain}")
            for key in sorted(entries):
                item = _mapping(entries[key], "document")
                doc_id = _text(item.get("doc_id"), "doc_id")
                item_domain = _text(item.get("domain"), "domain")
                if item_domain != domain:
                    raise BenchmarkSourceError("document domain does not match its container")
                spans = _mapping(item.get("spans"), "spans")
                documents.append(
                    BenchmarkDocument(
                        doc_id,
                        _text(item.get("title"), "title"),
                        domain,
                        _text(item.get("doc_text"), "doc_text"),
                        tuple(sorted(str(span_id) for span_id in spans)),
                    )
                )
        ids = [document.id for document in documents]
        if len(ids) != len(set(ids)):
            raise BenchmarkSourceError("benchmark document IDs are not globally unique")
        return tuple(documents)

    @staticmethod
    def _cases(
        payload: Mapping[str, Any], known_document_ids: set[str]
    ) -> tuple[BenchmarkCase, ...]:
        domain_map = _mapping(payload.get("dial_data"), "dial_data")
        cases: list[BenchmarkCase] = []
        for domain in sorted(domain_map):
            dialogues = _sequence(domain_map[domain], f"dial_data.{domain}")
            for raw_dialogue in dialogues:
                dialogue = _mapping(raw_dialogue, "dialogue")
                dialogue_id = _text(dialogue.get("dial_id"), "dial_id")
                turns = _sequence(dialogue.get("turns"), "turns")
                history: list[str] = []
                previous_user: str | None = None
                for raw_turn in turns:
                    turn = _mapping(raw_turn, "turn")
                    role = _text(turn.get("role"), "role")
                    utterance = _text(turn.get("utterance"), "utterance")
                    turn_id = _integer(turn.get("turn_id"), "turn_id")
                    if role == "user":
                        previous_user = utterance
                    elif role == "agent":
                        if previous_user is not None:
                            references = _sequence(turn.get("references"), "references")
                            document_ids: list[str] = []
                            span_ids: list[str] = []
                            for raw_reference in references:
                                reference = _mapping(raw_reference, "reference")
                                document_id = _text(
                                    reference.get("doc_id"), "reference.doc_id"
                                )
                                span_id = _text(reference.get("id_sp"), "reference.id_sp")
                                if document_id not in known_document_ids:
                                    raise BenchmarkSourceError(
                                        "dialogue references an unknown document"
                                    )
                                if document_id not in document_ids:
                                    document_ids.append(document_id)
                                span_ids.append(f"{document_id}:{span_id}")
                            if document_ids:
                                cases.append(
                                    BenchmarkCase(
                                        f"{dialogue_id}:{turn_id}",
                                        domain,
                                        dialogue_id,
                                        turn_id,
                                        previous_user,
                                        tuple(history),
                                        utterance,
                                        tuple(document_ids),
                                        tuple(span_ids),
                                    )
                                )
                        previous_user = None
                    else:
                        raise BenchmarkSourceError("dialogue turn role is invalid")
                    history.append(f"{role}: {utterance}")
        ids = [case.id for case in cases]
        if len(ids) != len(set(ids)):
            raise BenchmarkSourceError("benchmark case IDs are not unique")
        return tuple(cases)


class MultiDoc2DialRunner:
    """Checkpoint conversion work and label sample/full outputs without ambiguity."""

    async def run(
        self,
        dataset: BenchmarkDataset,
        *,
        mode: BenchmarkMode,
        max_cases: int | None,
        commit_sha: str,
        checkpoint_path: Path,
        output_path: Path,
        report_path: Path,
        processor: Callable[[BenchmarkCase], Awaitable[None]] | None = None,
        interrupt_after: int | None = None,
    ) -> BenchmarkConversionReport:
        selected = _select_cases(dataset.cases, mode, max_cases)
        fingerprint = _fingerprint(dataset, mode, max_cases, commit_sha)
        completed = _load_checkpoint(checkpoint_path, fingerprint)
        resumed_cases = len(completed)
        completed_set = set(completed)
        processed_now = 0
        for case in selected:
            if case.id in completed_set:
                continue
            if processor is not None:
                await processor(case)
            completed.append(case.id)
            completed_set.add(case.id)
            _write_json(
                checkpoint_path,
                {"fingerprint": fingerprint, "completed_case_ids": completed},
            )
            processed_now += 1
            if interrupt_after is not None and processed_now >= interrupt_after:
                raise BenchmarkInterrupted("benchmark run interrupted after checkpoint")
        selected_documents = {
            document_id for case in selected for document_id in case.expected_document_ids
        }
        output = {
            "schema_version": "1.0",
            "dataset": "MultiDoc2Dial",
            "dataset_revision": dataset.revision,
            "mode": mode.value,
            "is_full_dataset": mode is BenchmarkMode.FULL and len(selected) == len(dataset.cases),
            "documents": [
                asdict(document)
                for document in dataset.documents
                if document.id in selected_documents
            ],
            "cases": [asdict(case) for case in selected],
        }
        _write_json(output_path, output)
        output_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
        report = BenchmarkConversionReport(
            "1.0",
            "MultiDoc2Dial",
            dataset.revision,
            dataset.source_sha256,
            mode,
            mode is BenchmarkMode.FULL and len(selected) == len(dataset.cases),
            len(dataset.cases),
            len(selected),
            resumed_cases,
            commit_sha,
            output_sha256,
        )
        _write_json(report_path, _report_dict(report))
        return report


def _select_cases(
    cases: tuple[BenchmarkCase, ...], mode: BenchmarkMode, max_cases: int | None
) -> tuple[BenchmarkCase, ...]:
    if mode is BenchmarkMode.FULL:
        if max_cases is not None:
            raise ValueError("full benchmark mode does not accept max_cases")
        return cases
    if max_cases is None or max_cases <= 0:
        raise ValueError("sample benchmark mode requires a positive max_cases")
    return cases[:max_cases]


def _fingerprint(
    dataset: BenchmarkDataset, mode: BenchmarkMode, max_cases: int | None, commit_sha: str
) -> str:
    value = json.dumps(
        {
            "revision": dataset.revision,
            "source_sha256": dataset.source_sha256,
            "mode": mode.value,
            "max_cases": max_cases,
            "commit_sha": commit_sha,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _load_checkpoint(path: Path, fingerprint: str) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint:
            raise BenchmarkSourceError("benchmark checkpoint belongs to another run")
        values = payload["completed_case_ids"]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise BenchmarkSourceError("benchmark checkpoint is malformed")
        return list(values)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
        raise BenchmarkSourceError("benchmark checkpoint is malformed") from exc


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _report_dict(report: BenchmarkConversionReport) -> dict[str, object]:
    value = asdict(report)
    value["mode"] = report.mode.value
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BenchmarkSourceError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BenchmarkSourceError(f"{name} must be a sequence")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkSourceError(f"{name} must be non-empty text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BenchmarkSourceError(f"{name} must be a non-negative integer")
    return value
