"""Tesseract OCR adapter with explicit language capability checks."""

import asyncio
from typing import cast

import pytesseract  # type: ignore[import-untyped]
from PIL import Image
from pytesseract import TesseractError, TesseractNotFoundError

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind


class OcrError(AppError):
    """A sanitized OCR dependency or recognition failure."""


class TesseractOcrEngine:
    def __init__(
        self,
        *,
        languages: tuple[str, ...] = ("chi_sim", "eng"),
        page_segmentation_mode: int = 6,
    ) -> None:
        if not languages or any(not language.strip() for language in languages):
            raise ValueError("languages must not be empty")
        if not 0 <= page_segmentation_mode <= 13:
            raise ValueError("page_segmentation_mode must be between 0 and 13")
        self._languages = languages
        self._page_segmentation_mode = page_segmentation_mode
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.OCR,
            name="tesseract",
            version="5",
            capabilities=frozenset(self._languages),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.UNKNOWN,
        )

    async def recognize(self, image: Image.Image) -> str:
        if self._closed:
            raise RuntimeError("Tesseract OCR engine is closed")
        return await asyncio.to_thread(self._recognize_sync, image)

    async def aclose(self) -> None:
        self._closed = True

    def _recognize_sync(self, image: Image.Image) -> str:
        try:
            available = frozenset(pytesseract.get_languages(config=""))
            missing = tuple(language for language in self._languages if language not in available)
            if missing:
                raise OcrError(
                    ErrorCode.OCR_LANGUAGE_MISSING,
                    "Required OCR language data is not installed.",
                    {"missing_languages": missing},
                )
            text = pytesseract.image_to_string(
                image,
                lang="+".join(self._languages),
                config=f"--psm {self._page_segmentation_mode}",
            )
            return cast(str, text).strip()
        except TesseractNotFoundError as error:
            raise OcrError(
                ErrorCode.OCR_UNAVAILABLE,
                "The Tesseract executable is unavailable.",
            ) from error
        except TesseractError as error:
            raise OcrError(
                ErrorCode.OCR_FAILED,
                "Tesseract could not recognize the page.",
            ) from error
