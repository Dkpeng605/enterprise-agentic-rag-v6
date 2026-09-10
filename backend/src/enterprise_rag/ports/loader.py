"""Framework-neutral source, OCR, and Loader contracts."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from PIL import Image

from enterprise_rag.domain.common import (
    freeze_mapping,
    require_non_empty,
    require_sha256,
    require_uuid7,
)
from enterprise_rag.domain.documents import RootKind
from enterprise_rag.ports.provider import Provider


class BinarySource(Protocol):
    name: str
    media_type: str

    def chunks(self) -> AsyncIterator[bytes]: ...


@dataclass(frozen=True, slots=True)
class IngestionContext:
    tenant_id: UUID
    document_id: UUID
    version_id: UUID
    temporary_directory: Path

    def __post_init__(self) -> None:
        for name in ("tenant_id", "document_id", "version_id"):
            require_uuid7(getattr(self, name), name)
        if not self.temporary_directory.is_dir():
            raise ValueError("temporary_directory must be an existing directory")


@dataclass(frozen=True, slots=True)
class LoadedImage:
    page: int | None
    ordinal: int
    name: str
    media_type: str
    sha256: str
    width: int
    height: int
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (self.page is not None and self.page <= 0) or self.ordinal < 0:
            raise ValueError("image page must be positive when known and ordinal non-negative")
        require_non_empty(self.name, "name")
        require_non_empty(self.media_type, "media_type")
        require_sha256(self.sha256, "sha256")
        if self.width <= 0 or self.height <= 0 or not self.data:
            raise ValueError("loaded images require positive dimensions and data")


@dataclass(frozen=True, slots=True)
class LoadedRoot:
    ordinal: int
    kind: RootKind
    source_locator: Mapping[str, object]
    raw_text: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    images: tuple[LoadedImage, ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("root ordinal must not be negative")
        if not self.source_locator:
            raise ValueError("source_locator must not be empty")
        require_non_empty(self.raw_text, "raw_text")
        object.__setattr__(self, "source_locator", freeze_mapping(self.source_locator))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class OcrEngine(Provider, Protocol):
    async def recognize(self, image: Image.Image) -> str: ...


class Loader(Provider, Protocol):
    def supports(self, media_type: str, suffix: str) -> bool: ...

    async def load(self, source: BinarySource, context: IngestionContext) -> list[LoadedRoot]: ...
