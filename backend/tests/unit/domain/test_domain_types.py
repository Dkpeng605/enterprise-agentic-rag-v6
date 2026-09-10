import json
from collections.abc import MutableMapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import RFC_4122, UUID

import pytest

from enterprise_rag.domain import (
    AppError,
    Citation,
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    DocumentVisibility,
    ErrorCode,
    LeafChunk,
    QueryIntent,
    QueryMode,
    QueryPlan,
    QueryScope,
    RetrievalHit,
    RootChunk,
    RootKind,
    new_uuid7,
)

TENANT_ID = UUID("01900000-0000-7000-8000-000000000001")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000000002")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000000003")
VERSION_ID = UUID("01900000-0000-7000-8000-000000000004")
USER_ID = UUID("01900000-0000-7000-8000-000000000005")
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_root(*, revision: str = "revision-1", clean_text: str = "Clean text") -> RootChunk:
    return RootChunk.create(
        tenant_id=TENANT_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        index_revision=revision,
        ordinal=0,
        kind=RootKind.PAGE,
        source_locator={"page": 1},
        raw_text="Raw text",
        clean_text=clean_text,
        metadata={"headings": ["Intro"]},
    )


def make_leaf(root: RootChunk, *, text: str = "Leaf text", ordinal: int = 0) -> LeafChunk:
    return LeafChunk.create(
        root_id=root.id,
        tenant_id=TENANT_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        ordinal=ordinal,
        text=text,
        retrieval_text=text,
        start_offset=0,
        end_offset=len(text),
        token_count=2,
        metadata={"language": "en"},
    )


def test_uuid7_has_expected_version_variant_and_timestamp() -> None:
    timestamp_ms = 1_700_000_000_123
    identifier = new_uuid7(timestamp_ms=timestamp_ms)

    assert identifier.version == 7
    assert identifier.variant == RFC_4122
    assert identifier.int >> 80 == timestamp_ms


def test_document_serialization_uses_uuid_strings_enums_and_utc_iso8601() -> None:
    document = Document(
        id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        logical_name="policy",
        title="Policy",
        status=DocumentStatus.READY,
        active_version_id=VERSION_ID,
        visibility=DocumentVisibility.TENANT,
        created_by=USER_ID,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
    )

    serialized = document.to_dict()
    assert serialized["id"] == str(DOCUMENT_ID)
    assert serialized["status"] == "ready"
    assert serialized["created_at"] == "2026-01-02T03:04:05Z"
    json.dumps(serialized)


def test_document_is_frozen_and_rejects_non_utc_or_reversed_time() -> None:
    document = Document(
        id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        logical_name="policy",
        title="Policy",
        status=DocumentStatus.PENDING,
        active_version_id=None,
        visibility=DocumentVisibility.PRIVATE,
        created_by=USER_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    field_name = "title"
    with pytest.raises(FrozenInstanceError):
        setattr(document, field_name, "Changed")
    with pytest.raises(ValueError, match="UTC"):
        replace(document, created_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="precede"):
        replace(document, updated_at=NOW - timedelta(seconds=1))


def test_document_version_validates_hash_size_and_failure_state() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        DocumentVersion(
            id=VERSION_ID,
            document_id=DOCUMENT_ID,
            sha256="bad",
            source_name="policy.pdf",
            media_type="application/pdf",
            size_bytes=1,
            object_key="objects/policy.pdf",
            parser_provider="pdf",
            parser_version="1",
            status=DocumentVersionStatus.PENDING,
        )
    with pytest.raises(ValueError, match="error_code"):
        DocumentVersion(
            id=VERSION_ID,
            document_id=DOCUMENT_ID,
            sha256="a" * 64,
            source_name="policy.pdf",
            media_type="application/pdf",
            size_bytes=1,
            object_key="objects/policy.pdf",
            parser_provider="pdf",
            parser_version="1",
            status=DocumentVersionStatus.FAILED,
        )


def test_root_and_leaf_ids_are_stable_and_revision_sensitive() -> None:
    first_root = make_root()
    repeated_root = make_root()
    changed_revision = make_root(revision="revision-2")
    changed_content = make_root(clean_text="Different")

    assert first_root.id == repeated_root.id
    assert first_root.id != changed_revision.id
    assert first_root.id != changed_content.id

    first_leaf = make_leaf(first_root)
    repeated_leaf = make_leaf(repeated_root)
    assert first_leaf.id == repeated_leaf.id
    assert first_leaf.id != make_leaf(first_root, ordinal=1).id
    assert first_leaf.id != make_leaf(first_root, text="Different leaf").id

    with pytest.raises(ValueError, match="identity fields"):
        replace(first_root, id="root_" + "0" * 64)
    with pytest.raises(ValueError, match="identity fields"):
        replace(first_leaf, id="leaf_" + "0" * 64)


def test_chunk_metadata_is_deeply_immutable_and_serializable() -> None:
    root = make_root()
    headings = root.metadata["headings"]

    assert headings == ("Intro",)
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], root.metadata)["new"] = "value"
    json.dumps(root.to_dict())
    json.dumps(make_leaf(root).to_dict())


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_count": 0},
        {"start_offset": 2, "end_offset": 1},
        {"start_offset": None, "end_offset": 2},
    ],
)
def test_leaf_rejects_invalid_boundaries(overrides: dict[str, int | None]) -> None:
    root = make_root()
    with pytest.raises(ValueError):
        LeafChunk.create(
            root_id=root.id,
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            version_id=VERSION_ID,
            ordinal=0,
            text="Leaf text",
            retrieval_text="Leaf text",
            start_offset=overrides.get("start_offset", 0),
            end_offset=overrides.get("end_offset", 9),
            token_count=overrides.get("token_count", 2) or 0,
            metadata={},
        )


def test_query_plan_serializes_scope_without_mutable_lists() -> None:
    plan = QueryPlan(
        original_query="Compare A and B",
        rewritten_query="Compare policy A with policy B",
        intent=QueryIntent.COMPARISON,
        sub_queries=("What is A?", "What is B?"),
        requirements=("cite both",),
        scope=QueryScope(collection_ids=(COLLECTION_ID,), titles=("Policy",)),
        language="en",
        mode=QueryMode.DEEP,
    )

    assert plan.to_dict()["scope"] == {
        "collection_ids": [str(COLLECTION_ID)],
        "document_ids": [],
        "titles": ["Policy"],
        "organizations": [],
        "doc_types": [],
        "versions": [],
        "sections": [],
    }
    with pytest.raises(ValueError, match="duplicates"):
        QueryScope(titles=("same", "same"))


def test_hit_and_citation_validate_rank_score_and_source_boundaries() -> None:
    root = make_root()
    leaf = make_leaf(root)
    hit = RetrievalHit(
        leaf_id=leaf.id,
        root_id=root.id,
        dense_rank=1,
        sparse_rank=None,
        fused_score=0.5,
        rerank_score=None,
        selected=True,
    )
    citation = Citation(
        id=1,
        document_id=DOCUMENT_ID,
        root_id=root.id,
        chunk_ids=(leaf.id,),
        source_name="policy.pdf",
        title="Policy",
        page=1,
        section=None,
        quote="Leaf text",
        score=0.5,
    )

    assert hit.to_dict()["selected"] is True
    assert citation.to_dict()["chunk_ids"] == [leaf.id]
    with pytest.raises(ValueError, match="source rank"):
        RetrievalHit(leaf.id, root.id, None, None, 0.5, None, False)
    with pytest.raises(ValueError, match="finite"):
        RetrievalHit(leaf.id, root.id, 1, None, float("nan"), None, False)


def test_unified_error_shape_has_immutable_details_and_is_json_serializable() -> None:
    request_id = UUID("01900000-0000-7000-8000-000000000006")
    error = AppError(
        ErrorCode.DOCUMENT_UNSUPPORTED_TYPE,
        "The uploaded file type is not supported.",
        {"media_type": "application/octet-stream", "supported": ["application/pdf"]},
    )

    response = error.to_response(request_id).to_dict()
    assert response == {
        "error": {
            "code": "DOCUMENT_UNSUPPORTED_TYPE",
            "message": "The uploaded file type is not supported.",
            "request_id": str(request_id),
            "details": {
                "media_type": "application/octet-stream",
                "supported": ["application/pdf"],
            },
        }
    }
    assert "Traceback" not in json.dumps(response)
    with pytest.raises(AttributeError):
        error.message = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], error.details)["private"] = "leak"
