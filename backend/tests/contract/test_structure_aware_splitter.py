from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.domain import RootKind
from enterprise_rag.ports import CleanRoot, IngestionContext


@pytest.fixture
def context(tmp_path: Path) -> IngestionContext:
    return IngestionContext(
        tenant_id=UUID("01900000-0000-7000-8000-000000001001"),
        document_id=UUID("01900000-0000-7000-8000-000000001002"),
        version_id=UUID("01900000-0000-7000-8000-000000001003"),
        temporary_directory=tmp_path,
        index_revision="split-v1",
    )


def clean_root(text: str, *, kind: RootKind = RootKind.SECTION) -> CleanRoot:
    return CleanRoot(
        ordinal=0,
        kind=kind,
        source_locator={"section": "fixture"},
        raw_text=text,
        clean_text=text,
        metadata={"language": "mixed"},
    )


@pytest.mark.anyio
async def test_mixed_language_chunks_respect_limits_overlap_and_stable_ids(
    context: IngestionContext,
) -> None:
    text = "# 标题\n\n" + "检索 系统 Enterprise RAG provides evidence. " * 12
    splitter = StructureAwareSplitter(target_tokens=18, max_tokens=24, overlap_tokens=4)

    first = await splitter.split(clean_root(text), context)
    repeated = await splitter.split(clean_root(text), context)

    assert len(first.leaves) > 1
    assert first.root.id == repeated.root.id
    assert [leaf.id for leaf in first.leaves] == [leaf.id for leaf in repeated.leaves]
    assert all(leaf.token_count <= 24 for leaf in first.leaves)
    assert all(
        leaf.start_offset is not None and leaf.end_offset is not None for leaf in first.leaves
    )
    second_start = first.leaves[1].start_offset
    first_end = first.leaves[0].end_offset
    assert second_start is not None and first_end is not None
    assert second_start < first_end
    changed = await splitter.split(clean_root(text), replace(context, index_revision="split-v2"))
    assert changed.root.id != first.root.id
    assert changed.leaves[0].id != first.leaves[0].id


@pytest.mark.anyio
async def test_code_block_stays_whole_when_it_fits(context: IngestionContext) -> None:
    text = "Intro words here.\n\n```python\ndef answer():\n    return 'evidence'\n```\n\nEnd."
    result = await StructureAwareSplitter(target_tokens=12, max_tokens=30, overlap_tokens=2).split(
        clean_root(text), context
    )

    containing_code = [leaf.text for leaf in result.leaves if "def answer" in leaf.text]
    assert len(containing_code) == 1
    assert containing_code[0].count("```") == 2


@pytest.mark.anyio
async def test_table_continuations_repeat_header_within_max_tokens(
    context: IngestionContext,
) -> None:
    text = "| 名称 | 值 |\n| --- | --- |\n" + "\n".join(
        f"| 项目{i} | value{i} |" for i in range(12)
    )
    result = await StructureAwareSplitter(target_tokens=20, max_tokens=28, overlap_tokens=2).split(
        clean_root(text, kind=RootKind.SHEET_ROWS), context
    )

    assert len(result.leaves) > 1
    assert all(leaf.text.startswith("| 名称 | 值 |\n| --- | --- |") for leaf in result.leaves)
    assert all(leaf.token_count <= 28 for leaf in result.leaves)
    assert result.leaves[0].metadata["repeated_table_header"] is False
    assert all(leaf.metadata["repeated_table_header"] is True for leaf in result.leaves[1:])


@pytest.mark.anyio
async def test_very_long_unspaced_text_is_hard_split_and_close_is_idempotent(
    context: IngestionContext,
) -> None:
    splitter = StructureAwareSplitter(target_tokens=5, max_tokens=8, overlap_tokens=1)
    result = await splitter.split(clean_root("a" * 400), context)

    assert len(result.leaves) > 1
    assert all(leaf.token_count <= 8 for leaf in result.leaves)
    assert "".join(leaf.text for leaf in result.leaves).startswith("a" * 100)

    await splitter.aclose()
    await splitter.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await splitter.split(clean_root("text"), context)
