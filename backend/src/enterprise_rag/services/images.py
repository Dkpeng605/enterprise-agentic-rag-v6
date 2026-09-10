"""Persist extracted images and apply optional, explicitly degradable captions."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from enterprise_rag.domain.common import require_non_empty, require_sha256
from enterprise_rag.ports.loader import LoadedImage
from enterprise_rag.ports.object_store import ObjectStore
from enterprise_rag.ports.vision import CaptionStatus, VisionImage, VisionProvider


@dataclass(frozen=True, slots=True)
class EnrichedImage:
    page: int | None
    ordinal: int
    name: str
    media_type: str
    sha256: str
    width: int
    height: int
    object_key: str
    caption: str | None
    caption_status: CaptionStatus
    caption_error_code: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.name, "name")
        require_non_empty(self.media_type, "media_type")
        require_non_empty(self.object_key, "object_key")
        require_sha256(self.sha256, "sha256")
        if self.caption_status is CaptionStatus.CREATED and not self.caption:
            raise ValueError("created captions require text")
        if self.caption_status is CaptionStatus.DEGRADED and not self.caption_error_code:
            raise ValueError("degraded captions require an error code")
        if self.caption_status is not CaptionStatus.DEGRADED and self.caption_error_code:
            raise ValueError("only degraded captions may have an error code")


@dataclass(frozen=True, slots=True)
class ImageEnrichmentResult:
    images: tuple[EnrichedImage, ...]
    degraded: bool

    def __post_init__(self) -> None:
        if self.degraded != any(
            image.caption_status is CaptionStatus.DEGRADED for image in self.images
        ):
            raise ValueError("degraded flag must match image caption statuses")


class ImageEnricher:
    def __init__(self, object_store: ObjectStore, vision: VisionProvider) -> None:
        self._object_store = object_store
        self._vision = vision

    async def enrich(self, images: Sequence[LoadedImage]) -> ImageEnrichmentResult:
        enriched: list[EnrichedImage] = []
        for image in images:
            stored = await self._object_store.put(
                self._single_chunk(image.data), expected_sha256=image.sha256
            )
            caption: str | None = None
            caption_status = CaptionStatus.SKIPPED
            error_code: str | None = None
            try:
                candidate = await self._vision.caption(
                    VisionImage(
                        name=image.name,
                        media_type=image.media_type,
                        sha256=image.sha256,
                        width=image.width,
                        height=image.height,
                        data=image.data,
                    )
                )
                if candidate is not None and candidate.strip():
                    caption = candidate.strip()
                    caption_status = CaptionStatus.CREATED
            except Exception:
                caption_status = CaptionStatus.DEGRADED
                error_code = "VISION_CAPTION_FAILED"
            enriched.append(
                EnrichedImage(
                    page=image.page,
                    ordinal=image.ordinal,
                    name=image.name,
                    media_type=image.media_type,
                    sha256=image.sha256,
                    width=image.width,
                    height=image.height,
                    object_key=stored.key,
                    caption=caption,
                    caption_status=caption_status,
                    caption_error_code=error_code,
                )
            )
        result = tuple(enriched)
        return ImageEnrichmentResult(
            result,
            any(image.caption_status is CaptionStatus.DEGRADED for image in result),
        )

    @staticmethod
    async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
        yield data
