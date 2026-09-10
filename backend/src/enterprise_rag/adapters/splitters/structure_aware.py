"""Deterministic multilingual structure-aware Root/Leaf splitter."""

import bisect
import re
from dataclasses import dataclass

from enterprise_rag.domain.documents import LeafChunk, RootChunk, RootKind
from enterprise_rag.ports.cleaner import CleanRoot
from enterprise_rag.ports.loader import IngestionContext
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.splitter import SplitResult

_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9_]{1,20}|[^\s]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class _TokenSpan:
    start: int
    end: int


class StructureAwareSplitter:
    def __init__(
        self,
        *,
        target_tokens: int = 350,
        max_tokens: int = 480,
        overlap_tokens: int = 50,
    ) -> None:
        if target_tokens <= 0 or max_tokens <= 0:
            raise ValueError("token limits must be positive")
        if target_tokens > max_tokens:
            raise ValueError("target_tokens must not exceed max_tokens")
        if not 0 <= overlap_tokens < target_tokens:
            raise ValueError("overlap_tokens must be lower than target_tokens")
        self._target_tokens = target_tokens
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.SPLITTER,
            name="structure_aware",
            version="1",
            capabilities=frozenset(
                {"multilingual", "structure_boundaries", "overlap", "stable_ids", "tables"}
            ),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def split(self, root: CleanRoot, context: IngestionContext) -> SplitResult:
        if self._closed:
            raise RuntimeError("Splitter is closed")
        root_chunk = RootChunk.create(
            tenant_id=context.tenant_id,
            document_id=context.document_id,
            version_id=context.version_id,
            index_revision=context.index_revision,
            ordinal=root.ordinal,
            kind=root.kind,
            source_locator=root.source_locator,
            raw_text=root.raw_text,
            clean_text=root.clean_text,
            metadata=root.metadata,
        )
        tokens = self._tokenize(root.clean_text)
        if not tokens:
            raise ValueError("clean Root must contain tokens")
        boundaries = self._structural_token_boundaries(root.clean_text, tokens)
        header = self._table_header(root) if root.kind is RootKind.SHEET_ROWS else ""
        ranges = self._chunk_ranges(tokens, boundaries, header)
        leaves: list[LeafChunk] = []
        for ordinal, (start_token, end_token) in enumerate(ranges):
            start_offset = tokens[start_token].start
            end_offset = tokens[end_token - 1].end
            body = root.clean_text[start_offset:end_offset].strip()
            repeated_header = bool(header and start_token > 0 and not body.startswith(header))
            text = f"{header}\n{body}" if repeated_header else body
            leaves.append(
                LeafChunk.create(
                    root_id=root_chunk.id,
                    tenant_id=context.tenant_id,
                    document_id=context.document_id,
                    version_id=context.version_id,
                    ordinal=ordinal,
                    text=text,
                    retrieval_text=text,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    token_count=len(self._tokenize(text)),
                    metadata={
                        "source_locator": dict(root.source_locator),
                        "repeated_table_header": repeated_header,
                        "tokenizer": "deterministic-multilingual-v1",
                    },
                )
            )
        return SplitResult(root_chunk, tuple(leaves))

    async def aclose(self) -> None:
        self._closed = True

    def count_tokens(self, text: str) -> int:
        return len(self._tokenize(text))

    @staticmethod
    def _tokenize(text: str) -> tuple[_TokenSpan, ...]:
        return tuple(_TokenSpan(match.start(), match.end()) for match in _TOKEN.finditer(text))

    @staticmethod
    def _structural_token_boundaries(text: str, tokens: tuple[_TokenSpan, ...]) -> tuple[int, ...]:
        token_ends = [token.end for token in tokens]
        character_boundaries: set[int] = {len(text)}
        in_code = False
        offset = 0
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            offset += len(line)
            if stripped.startswith("```"):
                in_code = not in_code
                if not in_code:
                    character_boundaries.add(offset)
            elif not in_code and (
                not stripped
                or stripped.startswith(("#", "- ", "* ", "+ ", "> ", "|"))
                or re.match(r"\d+[.)]\s", stripped)
            ):
                character_boundaries.add(offset)
        return tuple(
            sorted(
                {
                    bisect.bisect_right(token_ends, boundary)
                    for boundary in character_boundaries
                    if bisect.bisect_right(token_ends, boundary) > 0
                }
            )
        )

    def _chunk_ranges(
        self,
        tokens: tuple[_TokenSpan, ...],
        boundaries: tuple[int, ...],
        table_header: str,
    ) -> tuple[tuple[int, int], ...]:
        total = len(tokens)
        header_tokens = self.count_tokens(table_header) if table_header else 0
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < total:
            header_cost = header_tokens if table_header and start > 0 else 0
            available_max = max(1, self._max_tokens - header_cost)
            available_target = max(1, min(self._target_tokens, available_max))
            hard_end = min(total, start + available_max)
            if hard_end == total:
                end = total
            else:
                minimum = min(hard_end, start + max(1, available_target // 2))
                candidates = [
                    boundary for boundary in boundaries if minimum <= boundary <= hard_end
                ]
                end = (
                    min(
                        candidates,
                        key=lambda boundary: (
                            abs((boundary - start) - available_target),
                            -boundary,
                        ),
                    )
                    if candidates
                    else min(hard_end, start + available_target)
                )
            if end <= start:
                end = min(total, start + 1)
            ranges.append((start, end))
            if end == total:
                break
            start = max(start + 1, end - self._overlap_tokens)
        return tuple(ranges)

    @staticmethod
    def _table_header(root: CleanRoot) -> str:
        lines = root.clean_text.splitlines()
        if len(lines) >= 2 and lines[0].lstrip().startswith("|") and "---" in lines[1]:
            return "\n".join(lines[:2])
        return ""
