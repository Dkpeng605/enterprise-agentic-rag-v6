from pathlib import Path
from uuid import UUID

import pytest

from enterprise_rag.adapters.cleaners import DeterministicCleaner
from enterprise_rag.domain import AppError, ErrorCode, RootKind
from enterprise_rag.ports import IngestionContext, LoadedRoot


@pytest.fixture
def context(tmp_path: Path) -> IngestionContext:
    return IngestionContext(
        tenant_id=UUID("01900000-0000-7000-8000-000000000901"),
        document_id=UUID("01900000-0000-7000-8000-000000000902"),
        version_id=UUID("01900000-0000-7000-8000-000000000903"),
        temporary_directory=tmp_path,
    )


def loaded(ordinal: int, text: str) -> LoadedRoot:
    return LoadedRoot(
        ordinal=ordinal,
        kind=RootKind.PAGE,
        source_locator={"page": ordinal + 1},
        raw_text=text,
        metadata={"source": "fixture"},
    )


@pytest.mark.anyio
async def test_cleaner_preserves_raw_text_and_audits_each_changed_rule(
    context: IngestionContext,
) -> None:
    raw = "\ufeff  En\u00adter\u200bprise\r\nknow-\nledge\t  base  \n\n\n\n"
    result = await DeterministicCleaner().clean(loaded(0, raw), context)

    assert result.root.raw_text == raw
    assert result.root.clean_text == "Enterprise\nknowledge base"
    assert result.root.metadata == {"source": "fixture"}
    assert [entry.rule for entry in result.audit] == [
        "nul_and_invisible_controls",
        "ocr_artifacts",
        "whitespace",
    ]
    assert all(entry.before_sha256 != entry.after_sha256 for entry in result.audit)


@pytest.mark.anyio
async def test_batch_removes_only_statistically_repeated_page_edges(
    context: IngestionContext,
) -> None:
    roots = [
        loaded(0, "Enterprise Manual\nFirst body\nConfidential"),
        loaded(1, "Enterprise Manual\nSecond body\nConfidential"),
        loaded(2, "Enterprise Manual\nThird body\nDifferent footer"),
    ]

    results = await DeterministicCleaner(repeated_edge_ratio=0.5).clean_all(roots, context)

    assert [result.root.clean_text for result in results] == [
        "First body",
        "Second body",
        "Third body\nDifferent footer",
    ]
    assert all("Enterprise Manual" in result.root.raw_text for result in results)
    assert [result.audit[-1].occurrences for result in results] == [2, 2, 1]


@pytest.mark.anyio
async def test_cleaning_is_idempotent(context: IngestionContext) -> None:
    cleaner = DeterministicCleaner()
    first = await cleaner.clean(loaded(0, "  one\t two  "), context)
    clean_as_loaded = LoadedRoot(
        ordinal=first.root.ordinal,
        kind=first.root.kind,
        source_locator=first.root.source_locator,
        raw_text=first.root.clean_text,
        metadata=first.root.metadata,
    )
    second = await cleaner.clean(clean_as_loaded, context)

    assert second.root.clean_text == first.root.clean_text
    assert second.audit == ()


@pytest.mark.anyio
async def test_empty_after_cleaning_and_closed_provider_are_explicit(
    context: IngestionContext,
) -> None:
    cleaner = DeterministicCleaner()
    with pytest.raises(AppError) as raised:
        await cleaner.clean(loaded(0, "\x00\u200b"), context)
    assert raised.value.code is ErrorCode.DOCUMENT_EMPTY

    await cleaner.aclose()
    await cleaner.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await cleaner.clean(loaded(0, "text"), context)
