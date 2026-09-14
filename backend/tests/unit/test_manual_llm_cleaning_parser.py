"""Strict response-validation tests for manual LLM cleaning."""

import json
from uuid import UUID

import pytest

from enterprise_rag.domain import AppError, ErrorCode, RootChunk, RootKind
from enterprise_rag.services.manual_llm_cleaning import parse_cleaning_response

TENANT_ID = UUID("01900000-0000-7000-8000-000000007101")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000007102")
VERSION_ID = UUID("01900000-0000-7000-8000-000000007103")


def root(text: str, *, ordinal: int = 0) -> RootChunk:
    return RootChunk.create(
        tenant_id=TENANT_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        index_revision="manual-cleaning-v1",
        ordinal=ordinal,
        kind=RootKind.SECTION,
        source_locator={"section": ordinal + 1},
        raw_text=text,
        clean_text=text,
    )


def response(*values: tuple[int, str]) -> str:
    return json.dumps(
        {"roots": [{"ordinal": ordinal, "clean_text": text} for ordinal, text in values]},
        ensure_ascii=False,
    )


def test_strict_response_accepts_noise_removal_and_restores_input_order() -> None:
    roots = (root("页眉\n\n正文  2026-09-14", ordinal=2), root("第二段", ordinal=7))

    cleaned = parse_cleaning_response(
        response((7, "第二段"), (2, "页眉\n正文 2026-09-14")), roots
    )

    assert cleaned == ("页眉\n正文 2026-09-14", "第二段")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("金额 199 元", "金额 299 元"),
        ("访问 https://example.test/a", "访问 https://example.test/b"),
        ('状态是 "ready"', '状态是 "failed"'),
        ("# 原始标题\n正文", "# 新标题\n正文"),
        ("系统必须保留原始策略", "系统必须删除原始策略"),
        ("第一条 第二条", "第二条 第一条"),
        ("```python\nprint(1)\n```", "```python\nprint(2)\n```"),
        ("| 名称 | 值 |\n| --- | --- |\n| A | 1 |", "| name | value |\n| --- | --- |\n| A | 1 |"),
    ],
)
def test_response_rejects_protected_anchor_changes(before: str, after: str) -> None:
    with pytest.raises(AppError) as raised:
        parse_cleaning_response(response((0, after)), (root(before),))

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE
    assert raised.value.details["root_ordinal"] == 0


def test_response_preserves_cleaning_output_whitespace_when_lexical_content_is_unchanged() -> None:
    roots = (root("  保留边界  "),)

    cleaned = parse_cleaning_response(response((0, " 保留边界 ")), roots)

    assert cleaned == (" 保留边界 ",)


def test_response_allows_only_repeated_edge_line_removal() -> None:
    roots = (root("页眉\n第一段", ordinal=0), root("页眉\n第二段", ordinal=1))

    cleaned = parse_cleaning_response(response((0, "第一段"), (1, "第二段")), roots)

    assert cleaned == ("第一段", "第二段")


def test_response_allows_pdf_layout_reflow_and_line_break_hyphen_repair() -> None:
    before = "#  标题\n\nPDF inter-\nface 内容。\n金额 2026。"
    after = "# 标题\nPDF interface 内容。金额 2026。"

    cleaned = parse_cleaning_response(response((0, after)), (root(before),))

    assert cleaned == (after,)


def test_response_rejects_merging_two_distinct_words() -> None:
    with pytest.raises(AppError) as raised:
        parse_cleaning_response(response((0, "helloworld")), (root("hello world"),))

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE


def test_response_cannot_remove_a_repeated_edge_line_from_the_middle() -> None:
    roots = (
        root("正文开始\n重复页眉\n正文结尾", ordinal=0),
        root("重复页眉\n第二个 Root", ordinal=1),
    )

    with pytest.raises(AppError) as raised:
        parse_cleaning_response(
            response((0, "正文开始\n正文结尾"), (1, "重复页眉\n第二个 Root")), roots
        )

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE


@pytest.mark.parametrize(
    "payload",
    [
        "```json\n{}\n```",
        json.dumps({"roots": []}),
        json.dumps({"roots": [{"ordinal": 0, "clean_text": "ok", "reason": "hidden"}]}),
        json.dumps({"roots": [{"ordinal": 1, "clean_text": "ok"}]}),
    ],
)
def test_response_rejects_non_strict_or_structurally_changed_json(payload: str) -> None:
    with pytest.raises(AppError) as raised:
        parse_cleaning_response(payload, (root("ok"),))

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE
