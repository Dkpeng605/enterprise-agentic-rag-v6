import io
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytesseract  # type: ignore[import-untyped]
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from enterprise_rag.adapters.loaders import PdfLoader
from enterprise_rag.adapters.ocr.tesseract import OcrError, TesseractOcrEngine
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.ports import IngestionContext
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "pdf"


class MemorySource:
    def __init__(
        self,
        data: bytes,
        *,
        name: str = "fixture.pdf",
        media_type: str = "application/pdf",
    ) -> None:
        self.data = data
        self.name = name
        self.media_type = media_type

    async def chunks(self) -> AsyncIterator[bytes]:
        midpoint = len(self.data) // 2
        yield self.data[:midpoint]
        yield self.data[midpoint:]


class FakeOcrEngine:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.OCR,
            "fake",
            "1",
            frozenset({"chi_sim", "eng"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def recognize(self, image: Image.Image) -> str:
        assert image.width > 0 and image.height > 0
        response = self.responses[self.calls] if self.calls < len(self.responses) else ""
        self.calls += 1
        return response

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def context(tmp_path: Path) -> IngestionContext:
    return IngestionContext(
        tenant_id=UUID("01900000-0000-7000-8000-000000000601"),
        document_id=UUID("01900000-0000-7000-8000-000000000602"),
        version_id=UUID("01900000-0000-7000-8000-000000000603"),
        temporary_directory=tmp_path,
    )


def make_pdf(*pages: str | None) -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792), pageCompression=0)
    for text in pages:
        if text is not None:
            pdf.setFont("Helvetica", 16)
            pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def encrypt_pdf(data: bytes) -> bytes:
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.anyio
async def test_text_pdf_preserves_one_based_pages_and_skips_empty_page(
    context: IngestionContext,
) -> None:
    ocr = FakeOcrEngine("")
    loader = PdfLoader(ocr, ocr_min_chars=10)

    roots = await loader.load(
        MemorySource(make_pdf("Policy page one", None, "Policy page three")), context
    )

    assert [root.source_locator["page"] for root in roots] == [1, 3]
    assert [root.ordinal for root in roots] == [0, 1]
    assert "Policy page one" in roots[0].raw_text
    assert "Policy page three" in roots[1].raw_text
    assert all(root.metadata["extraction"] == "text" for root in roots)
    assert ocr.calls == 1
    assert list(context.temporary_directory.iterdir()) == []


@pytest.mark.anyio
async def test_real_tesseract_recognizes_bilingual_scanned_pdf_and_records_image(
    context: IngestionContext,
) -> None:
    source = MemorySource((FIXTURE_ROOT / "scanned_zh_en.pdf").read_bytes())
    loader = PdfLoader(TesseractOcrEngine(), ocr_min_chars=1)
    try:
        roots = await loader.load(source, context)
    finally:
        await loader.aclose()

    assert len(roots) == 1
    assert roots[0].source_locator == {"page": 1}
    assert roots[0].metadata["extraction"] == "ocr"
    assert "企业知识库" in roots[0].raw_text
    assert "Enterprise RAG" in roots[0].raw_text
    assert roots[0].metadata["image_count"] == 1
    assert len(roots[0].images) == 1
    image = roots[0].images[0]
    assert image.page == 1
    assert image.sha256
    assert image.media_type.startswith("image/")
    assert image.width == 1800
    assert image.height == 500
    assert list(context.temporary_directory.iterdir()) == []


@pytest.mark.anyio
async def test_empty_encrypted_and_corrupt_pdfs_have_stable_errors_and_no_temp_files(
    context: IngestionContext,
) -> None:
    cases = (
        (make_pdf(None), ErrorCode.DOCUMENT_EMPTY, FakeOcrEngine("")),
        (
            encrypt_pdf(make_pdf("private")),
            ErrorCode.DOCUMENT_ENCRYPTED,
            FakeOcrEngine(),
        ),
        (b"%PDF-1.7\ntruncated", ErrorCode.DOCUMENT_CORRUPT, FakeOcrEngine()),
        (b"not a pdf", ErrorCode.DOCUMENT_CORRUPT, FakeOcrEngine()),
    )

    for data, expected_code, ocr in cases:
        with pytest.raises(AppError) as raised:
            await PdfLoader(ocr, ocr_min_chars=1).load(MemorySource(data), context)
        assert getattr(raised.value, "code", None) is expected_code
        assert list(context.temporary_directory.iterdir()) == []


@pytest.mark.anyio
async def test_loader_rejects_mismatched_media_type_or_suffix(
    context: IngestionContext,
) -> None:
    loader = PdfLoader(FakeOcrEngine())
    for source in (
        MemorySource(make_pdf("x"), name="x.txt"),
        MemorySource(make_pdf("x"), media_type="text/plain"),
    ):
        with pytest.raises(AppError) as raised:
            await loader.load(source, context)
        assert getattr(raised.value, "code", None) is ErrorCode.DOCUMENT_UNSUPPORTED_TYPE


@pytest.mark.anyio
async def test_tesseract_reports_missing_language_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng"])
    engine = TesseractOcrEngine(languages=("chi_sim", "eng"))
    image = Image.new("RGB", (100, 40), "white")
    try:
        with pytest.raises(OcrError) as raised:
            await engine.recognize(image)
    finally:
        image.close()

    assert raised.value.code is ErrorCode.OCR_LANGUAGE_MISSING
    assert raised.value.details == {"missing_languages": ("chi_sim",)}


@pytest.mark.anyio
async def test_close_is_idempotent_and_closes_owned_ocr_engine() -> None:
    ocr = FakeOcrEngine()
    loader = PdfLoader(ocr)

    await loader.aclose()
    await loader.aclose()

    assert ocr.closed
    assert loader.info().health is ProviderHealth.UNAVAILABLE
