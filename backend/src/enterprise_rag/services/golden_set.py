"""Strict loader and referential-integrity checks for versioned golden sets."""

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from enterprise_rag.domain.evaluation import EvaluationCase
from enterprise_rag.domain.golden_set import GoldenDocument, GoldenRoot, GoldenSet
from enterprise_rag.domain.retrieval import QueryMode


class GoldenSetValidationError(ValueError):
    """A stable error raised when a committed evaluation fixture is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Manifest(_StrictModel):
    schema_version: str
    revision: str
    cases_file: str
    corpus_file: str
    category_counts: dict[str, int]
    language_counts: dict[str, int]


class _Case(_StrictModel):
    id: str
    language: str
    category: str
    question: str
    mode: str
    allowed_collections: list[str]
    expected_document_ids: list[str]
    expected_root_ids: list[str]
    expected_facts: list[str]
    must_abstain: bool
    max_recovery_rounds: int
    tags: list[str]


class _CaseFile(_StrictModel):
    schema_version: str
    cases: list[_Case]


class _Root(_StrictModel):
    id: str
    text: str


class _Document(_StrictModel):
    id: str
    collection_id: str
    title: str
    roots: list[_Root]


class _CorpusFile(_StrictModel):
    schema_version: str
    documents: list[_Document]


class GoldenSetLoader:
    """Load one manifest without allowing references outside its directory."""

    def load(self, manifest_path: Path) -> GoldenSet:
        try:
            manifest = _Manifest.model_validate(_read_yaml(manifest_path))
            cases_path = _sibling(manifest_path, manifest.cases_file)
            corpus_path = _sibling(manifest_path, manifest.corpus_file)
            case_file = _CaseFile.model_validate(_read_yaml(cases_path))
            corpus_file = _CorpusFile.model_validate(_read_yaml(corpus_path))
            self._require_matching_schema(manifest, case_file, corpus_file)
            cases = tuple(self._to_case(item) for item in case_file.cases)
            documents = tuple(self._to_document(item) for item in corpus_file.documents)
            golden_set = GoldenSet(
                manifest.schema_version,
                manifest.revision,
                cases,
                documents,
            )
            self._validate_integrity(golden_set, manifest)
            return golden_set
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
            if isinstance(exc, GoldenSetValidationError):
                raise
            raise GoldenSetValidationError("invalid golden set fixture") from exc

    @staticmethod
    def _require_matching_schema(
        manifest: _Manifest, case_file: _CaseFile, corpus_file: _CorpusFile
    ) -> None:
        versions = {
            manifest.schema_version,
            case_file.schema_version,
            corpus_file.schema_version,
        }
        if len(versions) != 1:
            raise GoldenSetValidationError("golden set schema versions do not match")

    @staticmethod
    def _to_case(item: _Case) -> EvaluationCase:
        try:
            mode = QueryMode(item.mode)
        except ValueError as exc:
            raise GoldenSetValidationError("golden case has an unsupported query mode") from exc
        return EvaluationCase(
            item.id,
            item.language,
            item.category,
            item.question,
            mode,
            tuple(item.allowed_collections),
            tuple(item.expected_document_ids),
            tuple(item.expected_root_ids),
            tuple(item.expected_facts),
            item.must_abstain,
            item.max_recovery_rounds,
            tuple(item.tags),
        )

    @staticmethod
    def _to_document(item: _Document) -> GoldenDocument:
        return GoldenDocument(
            item.id,
            item.collection_id,
            item.title,
            tuple(GoldenRoot(root.id, root.text) for root in item.roots),
        )

    @staticmethod
    def _validate_integrity(golden_set: GoldenSet, manifest: _Manifest) -> None:
        case_ids = [case.id for case in golden_set.cases]
        document_ids = [document.id for document in golden_set.documents]
        roots = {
            root.id: (document, root)
            for document in golden_set.documents
            for root in document.roots
        }
        if len(case_ids) != len(set(case_ids)):
            raise GoldenSetValidationError("golden case IDs must be unique")
        if len(document_ids) != len(set(document_ids)):
            raise GoldenSetValidationError("golden document IDs must be unique")
        if len(roots) != sum(len(document.roots) for document in golden_set.documents):
            raise GoldenSetValidationError("golden root IDs must be globally unique")
        documents = {document.id: document for document in golden_set.documents}
        for case in golden_set.cases:
            _validate_case_references(case, documents, roots)
        if Counter(case.category for case in golden_set.cases) != Counter(
            manifest.category_counts
        ):
            raise GoldenSetValidationError("golden category coverage does not match manifest")
        if Counter(case.language for case in golden_set.cases) != Counter(
            manifest.language_counts
        ):
            raise GoldenSetValidationError("golden language coverage does not match manifest")


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sibling(manifest_path: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise GoldenSetValidationError("golden manifest file references must be local names")
    return manifest_path.parent / filename


def _validate_case_references(
    case: EvaluationCase,
    documents: dict[str, GoldenDocument],
    roots: dict[str, tuple[GoldenDocument, GoldenRoot]],
) -> None:
    if not case.must_abstain and (
        not case.expected_document_ids or not case.expected_root_ids
    ):
        raise GoldenSetValidationError("answerable golden cases require document and root IDs")
    for document_id in case.expected_document_ids:
        document = documents.get(document_id)
        if document is None:
            raise GoldenSetValidationError("golden case references an unknown document")
        if document.collection_id not in case.allowed_collections:
            raise GoldenSetValidationError("golden document is outside the case collection scope")
    expected_documents = set(case.expected_document_ids)
    selected_roots: list[GoldenRoot] = []
    for root_id in case.expected_root_ids:
        root_entry = roots.get(root_id)
        if root_entry is None:
            raise GoldenSetValidationError("golden case references an unknown root")
        document, root = root_entry
        if document.id not in expected_documents:
            raise GoldenSetValidationError("golden root does not belong to an expected document")
        selected_roots.append(root)
    source_text = "\n".join(root.text for root in selected_roots)
    if any(fact not in source_text for fact in case.expected_facts):
        raise GoldenSetValidationError("golden fact is absent from its expected roots")
