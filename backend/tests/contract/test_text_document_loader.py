import io
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from enterprise_rag.adapters.loaders import TextDocumentLoader
from enterprise_rag.domain import AppError, ErrorCode, RootKind
from enterprise_rag.ports import IngestionContext


class MemorySource:
    def __init__(self, data: bytes, *, name: str, media_type: str) -> None:
        self.data = data
        self.name = name
        self.media_type = media_type

    async def chunks(self) -> AsyncIterator[bytes]:
        yield self.data[:3]
        yield self.data[3:]


@pytest.fixture
def context(tmp_path: Path) -> IngestionContext:
    return IngestionContext(
        tenant_id=UUID("01900000-0000-7000-8000-000000000701"),
        document_id=UUID("01900000-0000-7000-8000-000000000702"),
        version_id=UUID("01900000-0000-7000-8000-000000000703"),
        temporary_directory=tmp_path,
    )


def make_docx() -> bytes:
    image_output = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(image_output, format="PNG")
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>产品说明</w:t></w:r></w:p>
  <w:p><w:r><w:t>中文正文 Enterprise RAG</w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>名称</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>值</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:t>模式</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>深度</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
 </w:body>
</w:document>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="image" Target="media/image1.png"/>
</Relationships>"""
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/media/image1.png", image_output.getvalue())
    return output.getvalue()


@pytest.mark.anyio
async def test_docx_preserves_heading_table_and_embedded_image(
    context: IngestionContext,
) -> None:
    roots = await TextDocumentLoader().load(
        MemorySource(
            make_docx(),
            name="产品.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        context,
    )

    assert len(roots) == 1
    root = roots[0]
    assert root.kind is RootKind.SECTION
    assert root.source_locator == {"section": "产品说明"}
    assert root.metadata["level"] == 1
    assert "中文正文 Enterprise RAG" in root.raw_text
    assert "| 名称 | 值 |" in root.raw_text
    assert "| 模式 | 深度 |" in root.raw_text
    assert len(root.images) == 1
    assert root.images[0].page is None
    assert root.images[0].width == 12
    assert root.images[0].height == 8
    assert root.images[0].media_type == "image/png"


@pytest.mark.anyio
async def test_html_converts_structure_and_never_exposes_or_fetches_active_content(
    context: IngestionContext,
) -> None:
    html = b"""<html><head><style>.secret{}</style><script>steal()</script></head>
<body><nav>hidden navigation</nav><h1>Guide</h1><p>Hello <code>RAG()</code></p>
<ul><li>first</li></ul><pre>if safe:\n  run()</pre>
<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
<img src="https://invalid.example/never.png" alt="diagram"></body></html>"""

    roots = await TextDocumentLoader().load(
        MemorySource(html, name="guide.html", media_type="text/html"), context
    )

    text = roots[0].raw_text
    assert "# Guide" in text
    assert "`RAG()`" in text
    assert "```" in text
    assert "| A | B |" in text
    assert "![diagram](https://invalid.example/never.png)" in text
    assert "steal" not in text and "hidden navigation" not in text and "secret" not in text
    assert roots[0].metadata["external_resources_not_fetched"] == 1


@pytest.mark.anyio
async def test_txt_and_markdown_require_utf8_and_preserve_content(
    context: IngestionContext,
) -> None:
    loader = TextDocumentLoader()
    txt = await loader.load(
        MemorySource("中文文本".encode(), name="note.txt", media_type="text/plain"), context
    )
    markdown = await loader.load(
        MemorySource(
            b"# Title\n\n```python\npass\n```",
            name="note.md",
            media_type="text/markdown",
        ),
        context,
    )
    assert txt[0].raw_text == "中文文本"
    assert markdown[0].raw_text.startswith("# Title")
    assert markdown[0].metadata["format"] == "markdown"

    with pytest.raises(AppError) as raised:
        await loader.load(MemorySource(b"\xff", name="bad.txt", media_type="text/plain"), context)
    assert raised.value.code is ErrorCode.DOCUMENT_ENCODING_INVALID


@pytest.mark.anyio
async def test_empty_corrupt_mismatched_and_closed_inputs_have_stable_behavior(
    context: IngestionContext,
) -> None:
    loader = TextDocumentLoader()
    cases = (
        (MemorySource(b"  ", name="empty.txt", media_type="text/plain"), ErrorCode.DOCUMENT_EMPTY),
        (
            MemorySource(b"not zip", name="bad.docx", media_type="application/zip"),
            ErrorCode.DOCUMENT_CORRUPT,
        ),
        (
            MemorySource(b"text", name="bad.html", media_type="text/plain"),
            ErrorCode.DOCUMENT_UNSUPPORTED_TYPE,
        ),
    )
    for source, code in cases:
        with pytest.raises(AppError) as raised:
            await loader.load(source, context)
        assert raised.value.code is code

    await loader.aclose()
    await loader.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await loader.load(MemorySource(b"x", name="x.txt", media_type="text/plain"), context)
