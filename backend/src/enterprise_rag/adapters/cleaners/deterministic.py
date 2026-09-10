"""Auditable and idempotent deterministic text normalization."""

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence

from enterprise_rag.domain.common import sha256_text
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.cleaner import Cleaner, CleaningAudit, CleanResult, CleanRoot
from enterprise_rag.ports.loader import IngestionContext, LoadedRoot
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

_INVISIBLE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2060\ufeff]")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_OCR_REPLACEMENTS = str.maketrans(
    {
        "\u00ad": "",
        "\u3000": " ",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬀ": "ff",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
    }
)
_HYPHENATED_LINE = re.compile(r"(?<=\w)-\n(?=\w)")


class CleanerError(AppError):
    """A stable client-safe cleaning failure."""


class DeterministicCleaner(Cleaner):
    def __init__(self, *, repeated_edge_ratio: float = 0.5) -> None:
        if not 0.5 <= repeated_edge_ratio <= 1.0:
            raise ValueError("repeated_edge_ratio must be between 0.5 and 1.0")
        self._repeated_edge_ratio = repeated_edge_ratio
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.CLEANER,
            name="deterministic",
            version="1",
            capabilities=frozenset(
                {"audit", "idempotent", "unicode", "whitespace", "repeated_edges"}
            ),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def clean(self, root: LoadedRoot, context: IngestionContext) -> CleanResult:
        return (await self.clean_all((root,), context))[0]

    async def clean_all(
        self, roots: Sequence[LoadedRoot], context: IngestionContext
    ) -> list[CleanResult]:
        del context
        if self._closed:
            raise RuntimeError("Cleaner is closed")
        base = [self._clean_root(root) for root in roots]
        if len(base) < 2:
            return base

        threshold = max(2, math.ceil(len(base) * self._repeated_edge_ratio))
        first_lines = Counter(
            self._edge_line(result.root.clean_text, first=True) for result in base
        )
        last_lines = Counter(
            self._edge_line(result.root.clean_text, first=False) for result in base
        )
        repeated_first = {
            line for line, count in first_lines.items() if line and count >= threshold
        }
        repeated_last = {line for line, count in last_lines.items() if line and count >= threshold}
        return [
            self._remove_repeated_edges(result, repeated_first, repeated_last) for result in base
        ]

    async def aclose(self) -> None:
        self._closed = True

    def _clean_root(self, root: LoadedRoot) -> CleanResult:
        value = root.raw_text
        audits: list[CleaningAudit] = []
        value = self._apply_rule("nul_and_invisible_controls", value, self._remove_controls, audits)
        value = self._apply_rule("ocr_artifacts", value, self._normalize_ocr, audits)
        value = self._apply_rule("whitespace", value, self._normalize_whitespace, audits)
        if not value.strip():
            raise CleanerError(
                ErrorCode.DOCUMENT_EMPTY,
                "The document contains no text after deterministic cleaning.",
            )
        return CleanResult(
            root=CleanRoot(
                ordinal=root.ordinal,
                kind=root.kind,
                source_locator=root.source_locator,
                raw_text=root.raw_text,
                clean_text=value,
                metadata=root.metadata,
                images=root.images,
            ),
            audit=tuple(audits),
        )

    @staticmethod
    def _apply_rule(
        name: str,
        value: str,
        operation: Callable[[str], tuple[str, int]],
        audits: list[CleaningAudit],
    ) -> str:
        updated, occurrences = operation(value)
        if occurrences:
            audits.append(
                CleaningAudit(name, occurrences, sha256_text(value), sha256_text(updated))
            )
        return updated

    @staticmethod
    def _remove_controls(value: str) -> tuple[str, int]:
        return _INVISIBLE_CONTROL.subn("", value)

    @staticmethod
    def _normalize_ocr(value: str) -> tuple[str, int]:
        translated = value.translate(_OCR_REPLACEMENTS)
        replacements = sum(value.count(chr(character)) for character in _OCR_REPLACEMENTS)
        joined, joined_count = _HYPHENATED_LINE.subn("", translated)
        return joined, replacements + joined_count

    @staticmethod
    def _normalize_whitespace(value: str) -> tuple[str, int]:
        normalized_newlines = value.replace("\r\n", "\n").replace("\r", "\n")
        changed_newlines = value.count("\r")
        lines: list[str] = []
        space_changes = 0
        for line in normalized_newlines.split("\n"):
            space_changes += sum(match.group() != " " for match in _SPACES.finditer(line))
            collapsed = _SPACES.sub(" ", line)
            stripped = collapsed.strip()
            space_changes += int(stripped != collapsed)
            lines.append(stripped)
        joined = "\n".join(lines).strip()
        compacted, blank_changes = _BLANK_LINES.subn("\n\n", joined)
        return compacted, changed_newlines + space_changes + blank_changes

    @staticmethod
    def _edge_line(value: str, *, first: bool) -> str:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return (lines[0] if first else lines[-1]) if lines else ""

    @staticmethod
    def _remove_repeated_edges(
        result: CleanResult,
        repeated_first: set[str],
        repeated_last: set[str],
    ) -> CleanResult:
        value = result.root.clean_text
        lines = value.splitlines()
        non_empty = [index for index, line in enumerate(lines) if line.strip()]
        remove: set[int] = set()
        if non_empty and lines[non_empty[0]].strip() in repeated_first:
            remove.add(non_empty[0])
        if non_empty and lines[non_empty[-1]].strip() in repeated_last:
            remove.add(non_empty[-1])
        if not remove:
            return result
        updated = "\n".join(line for index, line in enumerate(lines) if index not in remove).strip()
        if not updated:
            return result
        audit = CleaningAudit(
            "repeated_header_footer",
            len(remove),
            sha256_text(value),
            sha256_text(updated),
        )
        root = result.root
        return CleanResult(
            root=CleanRoot(
                ordinal=root.ordinal,
                kind=root.kind,
                source_locator=root.source_locator,
                raw_text=root.raw_text,
                clean_text=updated,
                metadata=root.metadata,
                images=root.images,
            ),
            audit=(*result.audit, audit),
        )
