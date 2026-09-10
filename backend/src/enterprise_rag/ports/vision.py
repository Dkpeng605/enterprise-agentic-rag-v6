"""Framework-neutral image captioning contract."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty, require_sha256
from enterprise_rag.ports.provider import Provider


@dataclass(frozen=True, slots=True)
class VisionImage:
    name: str
    media_type: str
    sha256: str
    width: int
    height: int
    data: bytes

    def __post_init__(self) -> None:
        require_non_empty(self.name, "name")
        require_non_empty(self.media_type, "media_type")
        require_sha256(self.sha256, "sha256")
        if self.width <= 0 or self.height <= 0 or not self.data:
            raise ValueError("VisionImage requires positive dimensions and data")


class CaptionStatus(StrEnum):
    CREATED = "created"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


class VisionProvider(Provider, Protocol):
    async def caption(self, image: VisionImage) -> str | None: ...
