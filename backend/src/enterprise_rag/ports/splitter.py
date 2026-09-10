"""Framework-neutral Root/Leaf splitting contract."""

from dataclasses import dataclass
from typing import Protocol

from enterprise_rag.domain.documents import LeafChunk, RootChunk
from enterprise_rag.ports.cleaner import CleanRoot
from enterprise_rag.ports.loader import IngestionContext
from enterprise_rag.ports.provider import Provider


@dataclass(frozen=True, slots=True)
class SplitResult:
    root: RootChunk
    leaves: tuple[LeafChunk, ...]

    def __post_init__(self) -> None:
        if not self.leaves:
            raise ValueError("split result must contain at least one Leaf")
        if any(leaf.root_id != self.root.id for leaf in self.leaves):
            raise ValueError("every Leaf must belong to the result Root")


class Splitter(Provider, Protocol):
    async def split(self, root: CleanRoot, context: IngestionContext) -> SplitResult: ...
