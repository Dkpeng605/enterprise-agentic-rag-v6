"""Deterministic multilingual paragraph and sentence aware Root/Leaf splitter."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from enterprise_rag.domain.documents import LeafChunk, RootChunk, RootKind
from enterprise_rag.ports.cleaner import CleanRoot
from enterprise_rag.ports.loader import IngestionContext
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.splitter import SplitResult

_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9_]{1,20}|[^\s]", re.UNICODE)
_STRUCTURAL_LINE = re.compile(r"(?:^#{1,6}\s|^[-*+]\s|^>\s|^\|\s*|^\d+[.)]\s)")
_CJK_SENTENCE_END = frozenset("。！？；")
_ASCII_SENTENCE_END = frozenset("!?;")
_CLOSING_PUNCTUATION = frozenset("\"'”’）】》」』)]}>")


@dataclass(frozen=True, slots=True)
class _TokenSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Segment:
    start: int
    end: int
    kind: str
    hard_cut: bool = False


@dataclass(frozen=True, slots=True)
class _Chunk:
    start: int
    end: int
    boundary: str
    hard_cut: bool


TokenCounter = Callable[[str], int]


class StructureAwareSplitter:
    def __init__(
        self,
        *,
        target_tokens: int = 350,
        max_tokens: int = 480,
        overlap_tokens: int = 50,
        token_counter: TokenCounter | None = None,
        tokenizer: str = "deterministic-multilingual-v1",
        embedding_token_limit: int | None = None,
        embedding_safety_margin: int = 1,
    ) -> None:
        if target_tokens <= 0 or max_tokens <= 0:
            raise ValueError("token limits must be positive")
        if embedding_token_limit is not None and embedding_token_limit <= 1:
            raise ValueError("embedding_token_limit must be greater than one")
        if embedding_safety_margin < 1:
            raise ValueError("embedding_safety_margin must be positive")
        configured_max_tokens = max_tokens
        if embedding_token_limit is not None:
            max_tokens = min(max_tokens, embedding_token_limit - embedding_safety_margin)
        if max_tokens <= 0:
            raise ValueError("effective max_tokens must be positive")
        self._configured_target_tokens = target_tokens
        self._configured_max_tokens = configured_max_tokens
        self._configured_overlap_tokens = overlap_tokens
        self._target_tokens = min(target_tokens, max_tokens)
        self._max_tokens = max_tokens
        self._overlap_tokens = min(overlap_tokens, max(0, self._target_tokens - 1))
        self._token_counter = token_counter or self._fallback_count
        self._tokenizer = tokenizer
        self._embedding_token_limit = embedding_token_limit
        self._embedding_safety_margin = embedding_safety_margin
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.SPLITTER,
            name="structure_aware",
            version="2",
            capabilities=frozenset(
                {
                    "multilingual",
                    "paragraph_boundaries",
                    "sentence_boundaries",
                    "structure_boundaries",
                    "overlap",
                    "stable_ids",
                    "tables",
                    "model_tokenizer",
                }
            ),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def split(self, root: CleanRoot, context: IngestionContext) -> SplitResult:
        if self._closed:
            raise RuntimeError("Splitter is closed")
        metadata = dict(root.metadata)
        header = self._table_header(root) if root.kind is RootKind.SHEET_ROWS else ""
        chunks = self._chunk_ranges(root.clean_text, header)
        hard_cut_count = sum(chunk.hard_cut for chunk in chunks)
        metadata["splitter"] = {
            "provider": "structure_aware",
            "version": "2",
            "settings": {
                "configured_target_tokens": self._configured_target_tokens,
                "configured_max_tokens": self._configured_max_tokens,
                "configured_overlap_tokens": self._configured_overlap_tokens,
                "target_tokens": self._target_tokens,
                "max_tokens": self._max_tokens,
                "overlap_tokens": self._overlap_tokens,
                "embedding_token_limit": self._embedding_token_limit,
                "embedding_safety_margin": self._embedding_safety_margin,
                "tokenizer": self._tokenizer,
                "boundary_policy": (
                    "code_block>table_row>heading/list>paragraph>sentence>token_hard_cut"
                ),
                "hard_cut_count": hard_cut_count,
            },
        }
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
            metadata=metadata,
        )
        leaves: list[LeafChunk] = []
        for ordinal, chunk in enumerate(chunks):
            body_start, body_end = self._trimmed_range(root.clean_text, chunk.start, chunk.end)
            body = root.clean_text[body_start:body_end]
            repeated_header = bool(header and ordinal > 0)
            text = f"{header}\n{body}" if repeated_header else body
            retrieval_text = text
            captions = root.metadata.get("image_captions", ())
            if ordinal == 0 and isinstance(captions, tuple):
                valid_captions = [value for value in captions if isinstance(value, str)]
                candidate = retrieval_text + "\n\n" + "\n".join(valid_captions)
                if valid_captions and self.count_tokens(candidate) <= self._max_tokens:
                    retrieval_text = candidate
            token_count = self.count_tokens(retrieval_text)
            if token_count > self._max_tokens:
                raise ValueError("splitter produced a Leaf above the embedding-safe token budget")
            leaves.append(
                LeafChunk.create(
                    root_id=root_chunk.id,
                    tenant_id=context.tenant_id,
                    document_id=context.document_id,
                    version_id=context.version_id,
                    ordinal=ordinal,
                    text=text,
                    retrieval_text=retrieval_text,
                    start_offset=body_start,
                    end_offset=body_end,
                    token_count=token_count,
                    metadata={
                        "source_locator": dict(root.source_locator),
                        "repeated_table_header": repeated_header,
                        "tokenizer": self._tokenizer,
                        "token_budget": self._max_tokens,
                        "boundary": chunk.boundary,
                        "hard_cut": chunk.hard_cut,
                    },
                )
            )
        return SplitResult(root_chunk, tuple(leaves))

    async def aclose(self) -> None:
        self._closed = True

    def count_tokens(self, text: str) -> int:
        return max(1, int(self._token_counter(text)))

    @staticmethod
    def _fallback_count(text: str) -> int:
        return len(_TOKEN.findall(text))

    def _chunk_ranges(self, text: str, table_header: str) -> tuple[_Chunk, ...]:
        segments = self._expand_overlong_segments(
            self._structure_segments(text), text, table_header
        )
        if not segments:
            raise ValueError("clean Root must contain tokens")
        header_cost = self.count_tokens(table_header) if table_header else 0
        if header_cost >= self._max_tokens:
            raise ValueError("table header must fit below the embedding token limit")

        chunks: list[_Chunk] = []
        start = 0
        while start < len(segments):
            repeated_header_cost = header_cost if table_header and start > 0 else 0
            end = start
            best_count = 0
            while end < len(segments):
                candidate = text[segments[start].start : segments[end].end].strip()
                candidate_count = self.count_tokens(candidate) + repeated_header_cost
                if candidate_count > self._max_tokens:
                    break
                best_count = candidate_count
                end += 1
                if candidate_count >= self._target_tokens:
                    break
            if end == start:
                # This should only be reachable when a custom tokenizer disagrees with its
                # own pre-splitting. Keep a safe progress path instead of looping forever.
                end = start + 1
                best_count = self.count_tokens(text[segments[start].start : segments[start].end])
                if best_count + repeated_header_cost > self._max_tokens:
                    raise ValueError("a single structural unit exceeds the embedding token limit")
            last = segments[end - 1]
            chunks.append(
                _Chunk(
                    start=segments[start].start,
                    end=last.end,
                    boundary="token_limit_hard_cut" if last.hard_cut else last.kind,
                    hard_cut=any(segment.hard_cut for segment in segments[start:end]),
                )
            )
            if end == len(segments):
                break
            next_start = self._overlap_start(segments, start, end, text)
            start = next_start if next_start < end else end
        return tuple(chunks)

    def _expand_overlong_segments(
        self, segments: tuple[_Segment, ...], text: str, table_header: str
    ) -> tuple[_Segment, ...]:
        header_cost = self.count_tokens(table_header) if table_header else 0
        available = self._max_tokens - header_cost
        expanded: list[_Segment] = []
        for segment in segments:
            value = text[segment.start : segment.end].strip()
            if self.count_tokens(value) + header_cost <= self._max_tokens:
                expanded.append(segment)
                continue
            expanded.extend(self._hard_split(segment, text, available))
        return tuple(expanded)

    def _hard_split(self, segment: _Segment, text: str, available: int) -> tuple[_Segment, ...]:
        if available <= 0:
            raise ValueError("a table header leaves no room for a table body")
        token_spans = self._tokenize(text[segment.start : segment.end])
        if not token_spans:
            return ()
        result: list[_Segment] = []
        cursor = 0
        while cursor < len(token_spans):
            furthest = cursor
            low, high = cursor + 1, len(token_spans)
            while low <= high:
                candidate_end = (low + high) // 2
                start = segment.start + token_spans[cursor].start
                end = segment.start + token_spans[candidate_end - 1].end
                if self.count_tokens(text[start:end]) <= available:
                    furthest = candidate_end
                    low = candidate_end + 1
                else:
                    high = candidate_end - 1
            if furthest == cursor:
                furthest = cursor + 1
            start = segment.start + token_spans[cursor].start
            end = segment.start + token_spans[furthest - 1].end
            result.append(_Segment(start, end, "token_hard_cut", True))
            cursor = furthest
        return tuple(result)

    def _overlap_start(
        self, segments: tuple[_Segment, ...], start: int, end: int, text: str
    ) -> int:
        if self._overlap_tokens <= 0:
            return end
        candidate = end
        for index in range(end - 1, start - 1, -1):
            value = text[segments[index].start : segments[end - 1].end].strip()
            if self.count_tokens(value) <= self._overlap_tokens:
                candidate = index
            else:
                break
        # Never reuse the entire current chunk: a short structural line can
        # otherwise make `start` repeat forever when it is itself below the
        # overlap budget.
        if start < candidate < end:
            return candidate
        # Prefer one complete sentence/paragraph over cutting its prefix. This is a
        # deliberate semantic-boundary trade-off: overlap is a soft upper bound.
        if end - start > 1 and segments[end - 1].kind in {"sentence", "paragraph"}:
            return end - 1
        return end

    @classmethod
    def _structure_segments(cls, text: str) -> tuple[_Segment, ...]:
        lines = list(re.finditer(r"[^\n]*(?:\n|$)", text))
        segments: list[_Segment] = []
        paragraph_start: int | None = None
        paragraph_end: int | None = None
        index = 0
        in_code = False
        code_start = 0

        def flush_paragraph() -> None:
            nonlocal paragraph_start, paragraph_end
            if paragraph_start is None or paragraph_end is None:
                return
            segments.extend(cls._sentence_segments(text, paragraph_start, paragraph_end))
            paragraph_start = None
            paragraph_end = None

        while index < len(lines):
            line = lines[index]
            value = line.group(0)
            stripped = value.strip()
            if in_code:
                if stripped.startswith("```"):
                    segments.append(_Segment(code_start, line.end(), "code_block"))
                    in_code = False
                index += 1
                continue
            if stripped.startswith("```"):
                flush_paragraph()
                in_code = True
                code_start = line.start()
                index += 1
                continue
            if not stripped:
                flush_paragraph()
                index += 1
                continue
            if _STRUCTURAL_LINE.match(stripped):
                flush_paragraph()
                segments.append(_Segment(line.start(), line.end(), "structure_line"))
                index += 1
                continue
            paragraph_start = line.start() if paragraph_start is None else paragraph_start
            paragraph_end = line.end()
            index += 1
        if in_code:
            # Preserve an unterminated code fence as one structural block; the loader
            # already accepted it and splitting must not silently discard its content.
            segments.append(_Segment(code_start, len(text), "code_block"))
        flush_paragraph()
        return tuple(segment for segment in segments if text[segment.start : segment.end].strip())

    @staticmethod
    def _sentence_segments(text: str, start: int, end: int) -> tuple[_Segment, ...]:
        result: list[_Segment] = []
        cursor = start
        index = start
        while index < end:
            char = text[index]
            is_terminal = char in _CJK_SENTENCE_END or (
                char in _ASCII_SENTENCE_END
                and (index + 1 == end or text[index + 1].isspace())
            )
            is_period = char == "." and (index + 1 == end or text[index + 1].isspace())
            if is_terminal or is_period:
                boundary = index + 1
                while boundary < end and text[boundary] in _CLOSING_PUNCTUATION:
                    boundary += 1
                if char in _CJK_SENTENCE_END or boundary == end or text[boundary].isspace():
                    result.append(_Segment(cursor, boundary, "sentence"))
                    cursor = boundary
                    index = boundary
                    continue
            index += 1
        if cursor < end:
            result.append(_Segment(cursor, end, "paragraph"))
        return tuple(result)

    @staticmethod
    def _tokenize(text: str) -> tuple[_TokenSpan, ...]:
        return tuple(_TokenSpan(match.start(), match.end()) for match in _TOKEN.finditer(text))

    @staticmethod
    def _trimmed_range(text: str, start: int, end: int) -> tuple[int, int]:
        value = text[start:end]
        leading = len(value) - len(value.lstrip())
        trailing = len(value.rstrip())
        return start + leading, start + trailing

    @staticmethod
    def _table_header(root: CleanRoot) -> str:
        lines = root.clean_text.splitlines()
        if len(lines) >= 2 and lines[0].lstrip().startswith("|") and "---" in lines[1]:
            return "\n".join(lines[:2])
        return ""
