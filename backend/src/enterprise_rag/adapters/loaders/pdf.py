"""Text and scanned PDF Loader with one-based source locations."""

import asyncio
import hashlib
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from enterprise_rag.domain.documents import RootKind
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.loader import (
    BinarySource,
    IngestionContext,
    LoadedImage,
    LoadedRoot,
    OcrEngine,
)
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind


class PdfLoaderError(AppError):
    """A stable client-safe PDF parsing failure."""


@dataclass(frozen=True, slots=True)
class _InspectedPage:
    text: str
    images: tuple[LoadedImage, ...]


class PdfLoader:
    def __init__(
        self,
        ocr_engine: OcrEngine,
        *,
        ocr_min_chars: int = 20,
        render_scale: float = 2.5,
    ) -> None:
        if ocr_min_chars < 0:
            raise ValueError("ocr_min_chars must not be negative")
        if not 1.0 <= render_scale <= 4.0:
            raise ValueError("render_scale must be between 1.0 and 4.0")
        self._ocr_engine = ocr_engine
        self._ocr_min_chars = ocr_min_chars
        self._render_scale = render_scale
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.LOADER,
            name="pdf",
            version="1",
            capabilities=frozenset({"application/pdf", "text", "ocr", "images"}),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    def supports(self, media_type: str, suffix: str) -> bool:
        return media_type.casefold() == "application/pdf" and suffix.casefold() == ".pdf"

    async def load(
        self, source: BinarySource, context: IngestionContext
    ) -> list[LoadedRoot]:
        if self._closed:
            raise RuntimeError("PDF Loader is closed")
        if not self.supports(source.media_type, Path(source.name).suffix):
            raise PdfLoaderError(
                ErrorCode.DOCUMENT_UNSUPPORTED_TYPE,
                "The source is not a supported PDF.",
                {"media_type": source.media_type},
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix="pdf-loader-", suffix=".pdf", dir=context.temporary_directory
        )
        temporary_path = Path(temporary_name)
        try:
            await self._write_source(descriptor, temporary_path, source)
            inspected = await asyncio.to_thread(self._inspect, temporary_path)
            roots: list[LoadedRoot] = []
            for page_index, page in enumerate(inspected):
                page_number = page_index + 1
                extracted_text = page.text.strip()
                text = extracted_text
                extraction = "text"
                if self._meaningful_characters(extracted_text) < self._ocr_min_chars:
                    image = await asyncio.to_thread(
                        self._render_page, temporary_path, page_index
                    )
                    try:
                        recognized = (await self._ocr_engine.recognize(image)).strip()
                    finally:
                        image.close()
                    if recognized:
                        text = recognized
                        extraction = "ocr"
                if not text.strip():
                    continue
                roots.append(
                    LoadedRoot(
                        ordinal=len(roots),
                        kind=RootKind.PAGE,
                        source_locator={"page": page_number},
                        raw_text=text,
                        metadata={
                            "page": page_number,
                            "extraction": extraction,
                            "image_count": len(page.images),
                        },
                        images=page.images,
                    )
                )
            if not roots:
                raise PdfLoaderError(
                    ErrorCode.DOCUMENT_EMPTY,
                    "The PDF contains no extractable or recognizable text.",
                )
            return roots
        except AppError:
            raise
        except PdfReadError as error:
            raise PdfLoaderError(
                ErrorCode.DOCUMENT_CORRUPT,
                "The PDF is malformed or truncated.",
            ) from error
        except Exception as error:
            raise PdfLoaderError(
                ErrorCode.DOCUMENT_CORRUPT,
                "The PDF could not be decoded.",
            ) from error
        finally:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._ocr_engine.aclose()
        self._closed = True

    @staticmethod
    async def _write_source(descriptor: int, path: Path, source: BinarySource) -> None:
        header = bytearray()
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in source.chunks():
                    if not isinstance(chunk, bytes):
                        raise TypeError("source chunks must be bytes")
                    if len(header) < 5:
                        header.extend(chunk[: 5 - len(header)])
                    await asyncio.to_thread(stream.write, chunk)
                await asyncio.to_thread(stream.flush)
                await asyncio.to_thread(os.fsync, stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if bytes(header) != b"%PDF-":
            raise PdfLoaderError(
                ErrorCode.DOCUMENT_CORRUPT,
                "The source does not have a PDF file signature.",
            )

    @classmethod
    def _inspect(cls, path: Path) -> tuple[_InspectedPage, ...]:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise PdfLoaderError(
                ErrorCode.DOCUMENT_ENCRYPTED,
                "Encrypted PDFs are not supported.",
            )
        pages: list[_InspectedPage] = []
        for page_number, page in enumerate(reader.pages, start=1):
            loaded_images: list[LoadedImage] = []
            for ordinal, image_file in enumerate(page.images):
                pil_image = image_file.image
                if pil_image is None:
                    pil_image = Image.open(BytesIO(image_file.data))
                try:
                    media_type = cls._image_media_type(image_file.name, pil_image)
                    loaded_images.append(
                        LoadedImage(
                            page=page_number,
                            ordinal=ordinal,
                            name=image_file.name,
                            media_type=media_type,
                            sha256=hashlib.sha256(image_file.data).hexdigest(),
                            width=pil_image.width,
                            height=pil_image.height,
                            data=image_file.data,
                        )
                    )
                finally:
                    pil_image.close()
            pages.append(_InspectedPage(page.extract_text() or "", tuple(loaded_images)))
        return tuple(pages)

    def _render_page(self, path: Path, page_index: int) -> Image.Image:
        document = pdfium.PdfDocument(path)
        try:
            page = document[page_index]
            try:
                bitmap = page.render(scale=self._render_scale)
                try:
                    return cast(Image.Image, bitmap.to_pil()).convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()

    @staticmethod
    def _image_media_type(name: str, image: Image.Image) -> str:
        guessed, _ = mimetypes.guess_type(name)
        if guessed is not None and guessed.startswith("image/"):
            return guessed
        if image.format:
            return f"image/{image.format.casefold()}"
        return "application/octet-stream"

    @staticmethod
    def _meaningful_characters(value: str) -> int:
        return sum(not character.isspace() for character in value)
