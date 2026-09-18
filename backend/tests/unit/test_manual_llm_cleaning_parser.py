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
        ("金额 199 元", "金额为 299 元"),
        ("访问 https://example.test/a", "访问地址：https://example.test/b"),
        ('状态是 "ready"', "当前状态为已就绪"),
        ("# 原始标题\n正文", "# 规范标题\n整理后的正文"),
        ("系统必须保留原始策略", "系统需要保留原始策略"),
        ("第一条 第二条", "第二条；第一条"),
        ("```python\nprint(1)\n```", "```python\nprint('1')\n```"),
        ("| 名称 | 值 |\n| --- | --- |\n| A | 1 |", "| 名称 | 数值 |\n| --- | --- |\n| A | 1 |"),
    ],
)
def test_response_accepts_trusted_llm_content_rewrites(before: str, after: str) -> None:
    assert parse_cleaning_response(response((0, after)), (root(before),)) == (after,)


def test_response_preserves_cleaning_output_whitespace_when_lexical_content_is_unchanged() -> None:
    roots = (root("  保留边界  "),)

    cleaned = parse_cleaning_response(response((0, " 保留边界 ")), roots)

    assert cleaned == (" 保留边界 ",)


def test_response_allows_repeated_edge_line_removal() -> None:
    roots = (root("页眉\n第一段", ordinal=0), root("页眉\n第二段", ordinal=1))

    cleaned = parse_cleaning_response(response((0, "第一段"), (1, "第二段")), roots)

    assert cleaned == ("第一段", "第二段")


def test_response_allows_layout_reflow_with_repeated_edge_removal() -> None:
    roots = (
        root("页眉\n第一段\n正文结尾", ordinal=0),
        root("页眉\n第二段", ordinal=1),
    )

    cleaned = parse_cleaning_response(
        response((0, "第一段 正文结尾"), (1, "第二段")), roots
    )

    assert cleaned == ("第一段 正文结尾", "第二段")


def test_response_allows_pdf_layout_reflow_and_line_break_hyphen_repair() -> None:
    before = "#  标题\n\nPDF inter-\nface 内容。\n金额 2026。"
    after = "# 标题\nPDF interface 内容。金额 2026。"

    cleaned = parse_cleaning_response(response((0, after)), (root(before),))

    assert cleaned == (after,)


def test_response_rejects_unbounded_root_expansion() -> None:
    with pytest.raises(AppError) as raised:
        parse_cleaning_response(response((0, "x" * 4_097)), (root("short"),))

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE


def test_response_accepts_rewritten_or_removed_middle_content() -> None:
    roots = (
        root("正文开始\n重复页眉\n正文结尾", ordinal=0),
        root("重复页眉\n第二个 Root", ordinal=1),
    )

    cleaned = parse_cleaning_response(
        response((0, "正文开始\n正文结尾"), (1, "重复页眉\n第二个 Root")), roots
    )

    assert cleaned == ("正文开始\n正文结尾", "重复页眉\n第二个 Root")


def test_response_accepts_explanatory_fields_around_required_result() -> None:
    payload = json.dumps(
        {
            "roots": [{"ordinal": 0, "clean_text": "整理后的内容", "reason": "修复 OCR"}],
            "summary": "cleaned",
        },
        ensure_ascii=False,
    )

    assert parse_cleaning_response(payload, (root("原始内容"),)) == ("整理后的内容",)


@pytest.mark.parametrize(
    "payload",
    [
        "```json\n{}\n```",
        json.dumps({"roots": []}),
        json.dumps({"roots": [{"ordinal": 1, "clean_text": "ok"}]}),
        json.dumps({"roots": [{"ordinal": 0, "clean_text": " "}]}),
        json.dumps(
            {"roots": [{"ordinal": 0, "clean_text": "one"}, {"ordinal": 0, "clean_text": "two"}]}
        ),
    ],
)
def test_response_rejects_non_strict_or_structurally_changed_json(payload: str) -> None:
    with pytest.raises(AppError) as raised:
        parse_cleaning_response(payload, (root("ok"),))

    assert raised.value.code is ErrorCode.LLM_INVALID_RESPONSE
