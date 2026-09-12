"""Acceptance checks for the committed bilingual golden set."""

from collections import Counter
from pathlib import Path
from shutil import copytree
from typing import Any

import pytest
import yaml

from enterprise_rag.services import GoldenSetLoader, GoldenSetValidationError

REPOSITORY_ROOT = Path(__file__).parents[3]
MANIFEST = REPOSITORY_ROOT / "evals/golden/v1/manifest.yaml"


def test_committed_golden_set_has_exact_category_and_language_coverage() -> None:
    golden_set = GoldenSetLoader().load(MANIFEST)

    assert golden_set.schema_version == "1.0"
    assert golden_set.revision == "enterprise-demo-golden-2026-09-09"
    assert len(golden_set.cases) == 30
    assert Counter(case.language for case in golden_set.cases) == {"zh": 15, "en": 15}
    assert Counter(case.category for case in golden_set.cases) == {
        "keyword": 6,
        "paraphrase": 6,
        "comparison": 5,
        "metadata_scope": 4,
        "table": 3,
        "ocr": 2,
        "unanswerable": 4,
    }
    assert len(golden_set.documents) == 12
    assert len(golden_set.roots) == 21


def test_every_answerable_case_references_scoped_source_text() -> None:
    golden_set = GoldenSetLoader().load(MANIFEST)
    documents = {document.id: document for document in golden_set.documents}
    roots = {
        root.id: (document.id, document.collection_id, root.text)
        for document in golden_set.documents
        for root in document.roots
    }

    for case in golden_set.cases:
        if case.must_abstain:
            assert not case.expected_document_ids
            assert not case.expected_root_ids
            assert not case.expected_facts
            continue
        assert case.expected_document_ids
        assert case.expected_root_ids
        selected_text = []
        for root_id in case.expected_root_ids:
            document_id, collection_id, text = roots[root_id]
            assert document_id in case.expected_document_ids
            assert collection_id in case.allowed_collections
            assert documents[document_id].collection_id == collection_id
            selected_text.append(text)
        assert all(fact in "\n".join(selected_text) for fact in case.expected_facts)


@pytest.mark.parametrize("mutation", ["unknown_field", "unknown_root", "wrong_coverage"])
def test_loader_rejects_schema_reference_and_coverage_drift(
    tmp_path: Path, mutation: str
) -> None:
    fixture = tmp_path / "v1"
    copytree(MANIFEST.parent, fixture)
    cases_path = fixture / "cases.yaml"
    manifest_path = fixture / "manifest.yaml"

    if mutation == "wrong_coverage":
        payload = _load_yaml(manifest_path)
        payload["category_counts"]["keyword"] = 5
        _write_yaml(manifest_path, payload)
    else:
        payload = _load_yaml(cases_path)
        if mutation == "unknown_field":
            payload["cases"][0]["surprise"] = True
        else:
            payload["cases"][0]["expected_root_ids"] = ["root_missing"]
        _write_yaml(cases_path, payload)

    with pytest.raises(GoldenSetValidationError):
        GoldenSetLoader().load(manifest_path)


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
