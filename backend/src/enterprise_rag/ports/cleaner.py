"""Framework-neutral deterministic text-cleaning contracts."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from enterprise_rag.domain.common import freeze_mapping, require_non_empty, require_sha256
from enterprise_rag.domain.documents import RootKind
from enterprise_rag.ports.loader import IngestionContext, LoadedImage, LoadedRoot
from enterprise_rag.ports.provider import Provider


@dataclass(frozen=True, slots=True)
class CleaningAudit:
    rule: str
    occurrences: int
    before_sha256: str
    after_sha256: str

    def __post_init__(self) -> None:
        require_non_empty(self.rule, "rule")
        if self.occurrences <= 0:
            raise ValueError("cleaning audit occurrences must be positive")
        require_sha256(self.before_sha256, "before_sha256")
        require_sha256(self.after_sha256, "after_sha256")


@dataclass(frozen=True, slots=True)
class CleanRoot:
    ordinal: int
    kind: RootKind
    source_locator: Mapping[str, object]
    raw_text: str
    clean_text: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    images: tuple[LoadedImage, ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("root ordinal must not be negative")
        if not self.source_locator:
            raise ValueError("source_locator must not be empty")
        require_non_empty(self.raw_text, "raw_text")
        require_non_empty(self.clean_text, "clean_text")
        object.__setattr__(self, "source_locator", freeze_mapping(self.source_locator))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class CleanResult:
    root: CleanRoot
    audit: tuple[CleaningAudit, ...]


class Cleaner(Provider, Protocol):
    async def clean(self, root: LoadedRoot, context: IngestionContext) -> CleanResult: ...

    async def clean_all(
        self, roots: Sequence[LoadedRoot], context: IngestionContext
    ) -> list[CleanResult]: ...
