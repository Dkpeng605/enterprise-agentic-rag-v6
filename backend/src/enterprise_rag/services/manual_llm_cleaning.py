"""Explicit, bounded, one-pass LLM cleaning for an indexed document version."""

import asyncio
import json
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.ingestion import IngestionContentRepository
from enterprise_rag.adapters.database.models import (
    DocumentModel,
    DocumentVersionModel,
    LeafModel,
    RootModel,
)
from enterprise_rag.domain.common import require_utc, sha256_text
from enterprise_rag.domain.documents import LeafChunk, RootChunk, RootKind
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.cleaner import CleanRoot
from enterprise_rag.ports.llm import CompletionRequest, LanguageModel
from enterprise_rag.ports.loader import IngestionContext
from enterprise_rag.ports.splitter import Splitter
from enterprise_rag.ports.vector_store import VectorStore
from enterprise_rag.services.projection import ProjectionRequest, ProjectionService

_FACT_ANCHOR = re.compile(
    r"https?://[^\s<>)]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?<![\w])[-+]?\d+(?:[.,:/-]\d+)*(?:%|[A-Za-z]+)?(?![\w])|"
    r"`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|“[^”\n]+”|‘[^’\n]+’"
)
_FENCE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_LEXICAL_TOKEN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:[.,:/-]\d+)*"
)


@dataclass(frozen=True, slots=True)
class LlmCleaningPreflight:
    document_id: UUID
    version_id: UUID
    available: bool
    reason: str | None
    provider: str
    model: str
    remote: bool
    root_count: int
    input_chars: int
    max_roots: int
    max_input_chars: int
    estimated_calls: int
    max_output_tokens: int
    already_applied: bool


@dataclass(frozen=True, slots=True)
class LlmCleaningResult:
    document_id: UUID
    version_id: UUID
    provider: str
    model: str
    root_count: int
    changed_root_count: int
    leaf_count_before: int
    leaf_count_after: int
    input_chars: int
    output_chars: int
    input_tokens: int
    output_tokens: int
    retry_count: int
    llm_calls: int
    applied_at: datetime


@dataclass(frozen=True, slots=True)
class _DocumentSnapshot:
    tenant_id: UUID
    collection_id: UUID
    document_id: UUID
    version_id: UUID
    parser_provider: str
    parser_version: str
    document_status: str
    version_status: str
    roots: tuple[RootChunk, ...]
    leaves: tuple[LeafChunk, ...]
    fingerprint: str


class ManualLlmCleaningService:
    """Run one confirmed remote cleaning call and atomically replace indexed content.

    The version lock is process-local by design. The Mac runtime is single-process;
    a multi-replica deployment must replace it with distributed job coordination.
    """

    def __init__(
        self,
        *,
        database: Database,
        language_model: LanguageModel,
        splitter: Splitter,
        projection: ProjectionService,
        vector_store: VectorStore,
        temporary_root: Path,
        max_roots: int = 20,
        max_input_chars: int = 12_000,
        max_output_tokens: int = 8_000,
    ) -> None:
        if min(max_roots, max_input_chars, max_output_tokens) <= 0:
            raise ValueError("LLM cleaning limits must be positive")
        provider = language_model.info()
        if not provider.is_remote:
            raise ValueError("manual LLM cleaning requires a remote provider disclosure")
        self._database = database
        self._language_model = language_model
        self._splitter = splitter
        self._projection = projection
        self._vector_store = vector_store
        self._temporary_root = temporary_root
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        self._max_roots = max_roots
        self._max_input_chars = max_input_chars
        self._max_output_tokens = max_output_tokens
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def preflight(self, tenant_id: UUID, document_id: UUID) -> LlmCleaningPreflight:
        snapshot = await self._load_snapshot(tenant_id, document_id, require_ready=False)
        root_count = len(snapshot.roots)
        input_chars = sum(len(root.clean_text) for root in snapshot.roots)
        already_applied = any("llm_cleaning" in root.metadata for root in snapshot.roots)
        reason: str | None = None
        if snapshot.document_status != "ready" or snapshot.version_status != "indexed":
            reason = "The active document version is not ready for LLM cleaning."
        elif already_applied:
            reason = "This document version has already received its one LLM cleaning pass."
        elif root_count > self._max_roots:
            reason = "The document has more Roots than the one-call cleaning limit."
        elif input_chars > self._max_input_chars:
            reason = "The document exceeds the one-call cleaning character limit."
        elif not snapshot.roots:
            reason = "The document has no indexed Roots to clean."
        info = self._language_model.info()
        return LlmCleaningPreflight(
            document_id,
            snapshot.version_id,
            reason is None,
            reason,
            info.name,
            info.version,
            info.is_remote,
            root_count,
            input_chars,
            self._max_roots,
            self._max_input_chars,
            1,
            self._max_output_tokens,
            already_applied,
        )

    async def clean(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        document_id: UUID,
        expected_version_id: UUID,
        confirm_remote_processing: bool,
        now: datetime,
    ) -> LlmCleaningResult:
        require_utc(now, "now")
        if not confirm_remote_processing:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "Remote LLM processing must be explicitly confirmed.",
            )
        lock = self._locks.setdefault(expected_version_id, asyncio.Lock())
        if lock.locked():
            raise AppError(
                ErrorCode.CONFLICT,
                "An LLM cleaning pass is already running for this document version.",
            )
        async with lock:
            snapshot = await self._load_snapshot(tenant_id, document_id, require_ready=True)
            if snapshot.version_id != expected_version_id:
                raise AppError(
                    ErrorCode.CONFLICT,
                    "The active document version changed after preflight.",
                    {"active_version_id": str(snapshot.version_id)},
                )
            preflight = await self._preflight_snapshot(snapshot)
            if not preflight.available:
                raise AppError(
                    ErrorCode.CONFLICT,
                    preflight.reason or "This document version cannot be cleaned.",
                )
            completion = await self._language_model.complete(
                CompletionRequest(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=_request_json(snapshot.roots),
                    max_output_tokens=self._max_output_tokens,
                )
            )
            cleaned_texts = parse_cleaning_response(completion.text, snapshot.roots)
            roots, leaves = await self._resplit(
                snapshot,
                cleaned_texts,
                actor_id=actor_id,
                now=now,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                retry_count=completion.retry_count,
            )
            changed = sum(
                old.clean_text != new.clean_text
                for old, new in zip(snapshot.roots, roots, strict=True)
            )
            started = False
            try:
                await self._mark_processing(snapshot)
                started = True
                await self._vector_store.delete_by_version(tenant_id, snapshot.version_id)
                await self._projection.project(self._projection_request(snapshot, leaves))
                await self._persist(snapshot, roots, leaves)
            except BaseException as primary:
                if started:
                    try:
                        await self._restore(snapshot)
                    except Exception as recovery_error:
                        await self._mark_failed(snapshot)
                        raise AppError(
                            ErrorCode.PROJECTION_CLEANUP_FAILED,
                            "LLM cleaning failed and the previous index could not be restored.",
                        ) from recovery_error
                raise primary
            info = self._language_model.info()
            return LlmCleaningResult(
                document_id,
                snapshot.version_id,
                info.name,
                info.version,
                len(roots),
                changed,
                len(snapshot.leaves),
                len(leaves),
                sum(len(root.clean_text) for root in snapshot.roots),
                sum(len(root.clean_text) for root in roots),
                completion.input_tokens,
                completion.output_tokens,
                completion.retry_count,
                1,
                now,
            )

    async def _preflight_snapshot(self, snapshot: _DocumentSnapshot) -> LlmCleaningPreflight:
        root_count = len(snapshot.roots)
        input_chars = sum(len(root.clean_text) for root in snapshot.roots)
        already_applied = any("llm_cleaning" in root.metadata for root in snapshot.roots)
        reason = None
        if snapshot.document_status != "ready" or snapshot.version_status != "indexed":
            reason = "The active document version is not ready for LLM cleaning."
        elif already_applied:
            reason = "This document version has already received its one LLM cleaning pass."
        elif not snapshot.roots:
            reason = "The document has no indexed Roots to clean."
        elif root_count > self._max_roots:
            reason = "The document has more Roots than the one-call cleaning limit."
        elif input_chars > self._max_input_chars:
            reason = "The document exceeds the one-call cleaning character limit."
        info = self._language_model.info()
        return LlmCleaningPreflight(
            snapshot.document_id,
            snapshot.version_id,
            reason is None,
            reason,
            info.name,
            info.version,
            True,
            root_count,
            input_chars,
            self._max_roots,
            self._max_input_chars,
            1,
            self._max_output_tokens,
            already_applied,
        )

    async def _load_snapshot(
        self, tenant_id: UUID, document_id: UUID, *, require_ready: bool
    ) -> _DocumentSnapshot:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(DocumentModel, DocumentVersionModel)
                    .join(
                        DocumentVersionModel,
                        DocumentVersionModel.id == DocumentModel.active_version_id,
                    )
                    .where(
                        DocumentModel.id == document_id,
                        DocumentModel.tenant_id == tenant_id,
                        DocumentModel.status != "deleted",
                    )
                )
            ).one_or_none()
            if row is None:
                raise AppError(ErrorCode.NOT_FOUND, "The document was not found.")
            document, version = row
            if require_ready and (document.status != "ready" or version.status != "indexed"):
                raise AppError(
                    ErrorCode.CONFLICT,
                    "The active document version is not ready for LLM cleaning.",
                )
            roots = tuple(
                await session.scalars(
                    select(RootModel)
                    .where(RootModel.version_id == version.id)
                    .order_by(RootModel.ordinal, RootModel.id)
                )
            )
            leaves = tuple(
                await session.scalars(
                    select(LeafModel)
                    .where(LeafModel.version_id == version.id)
                    .order_by(LeafModel.root_id, LeafModel.ordinal, LeafModel.id)
                )
            )
        domain_roots = tuple(_root_from_model(root) for root in roots)
        domain_leaves = tuple(_leaf_from_model(leaf) for leaf in leaves)
        return _DocumentSnapshot(
            tenant_id,
            document.collection_id,
            document.id,
            version.id,
            version.parser_provider,
            version.parser_version,
            document.status,
            version.status,
            domain_roots,
            domain_leaves,
            _fingerprint(domain_roots),
        )

    async def _resplit(
        self,
        snapshot: _DocumentSnapshot,
        cleaned_texts: Sequence[str],
        *,
        actor_id: UUID,
        now: datetime,
        input_tokens: int,
        output_tokens: int,
        retry_count: int,
    ) -> tuple[tuple[RootChunk, ...], tuple[LeafChunk, ...]]:
        info = self._language_model.info()
        input_chars = sum(len(root.clean_text) for root in snapshot.roots)
        output_chars = sum(len(value) for value in cleaned_texts)
        changed_root_count = sum(
            root.clean_text != value
            for root, value in zip(snapshot.roots, cleaned_texts, strict=True)
        )
        roots: list[RootChunk] = []
        leaves: list[LeafChunk] = []
        with tempfile.TemporaryDirectory(dir=self._temporary_root) as directory:
            context = IngestionContext(
                snapshot.tenant_id,
                snapshot.document_id,
                snapshot.version_id,
                Path(directory),
                snapshot.roots[0].index_revision,
            )
            for old, clean_text in zip(snapshot.roots, cleaned_texts, strict=True):
                before = sha256_text(old.clean_text)
                after = sha256_text(clean_text)
                metadata = dict(old.metadata)
                cleaning = dict(_mapping(metadata.get("cleaning")))
                audit = list(cleaning.get("audit", []))
                if old.clean_text != clean_text:
                    audit.append(
                        {
                            "rule": "manual_llm_cleaning",
                            "occurrences": 1,
                            "before_sha256": before,
                            "after_sha256": after,
                        }
                    )
                cleaning["audit"] = audit
                metadata["cleaning"] = cleaning
                metadata["llm_cleaning"] = {
                    "provider": info.name,
                    "model": info.version,
                    "remote": info.is_remote,
                    "applied_at": now.isoformat(),
                    "actor_id": str(actor_id),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "input_chars": input_chars,
                    "output_chars": output_chars,
                    "root_count": len(snapshot.roots),
                    "changed_root_count": changed_root_count,
                    "llm_calls": 1,
                    "retry_count": retry_count,
                    "before_sha256": before,
                    "after_sha256": after,
                    "changed": old.clean_text != clean_text,
                }
                result = await self._splitter.split(
                    CleanRoot(
                        old.ordinal,
                        old.kind,
                        old.source_locator,
                        old.raw_text,
                        clean_text,
                        metadata,
                    ),
                    context,
                )
                roots.append(result.root)
                leaves.extend(result.leaves)
        return tuple(roots), tuple(leaves)

    async def _mark_processing(self, snapshot: _DocumentSnapshot) -> None:
        async with self._database.session() as session:
            document = await session.scalar(
                select(DocumentModel)
                .where(
                    DocumentModel.id == snapshot.document_id,
                    DocumentModel.tenant_id == snapshot.tenant_id,
                )
                .with_for_update()
            )
            version = await session.scalar(
                select(DocumentVersionModel)
                .where(DocumentVersionModel.id == snapshot.version_id)
                .with_for_update()
            )
            roots = tuple(
                await session.scalars(
                    select(RootModel)
                    .where(RootModel.version_id == snapshot.version_id)
                    .order_by(RootModel.ordinal, RootModel.id)
                )
            )
            if (
                document is None
                or version is None
                or document.active_version_id != snapshot.version_id
                or document.status != "ready"
                or version.status != "indexed"
                or _fingerprint(tuple(_root_from_model(root) for root in roots))
                != snapshot.fingerprint
            ):
                raise AppError(
                    ErrorCode.CONFLICT,
                    "The document changed after LLM cleaning preflight.",
                )
            document.status = "processing"
            version.status = "processing"

    async def _persist(
        self,
        snapshot: _DocumentSnapshot,
        roots: Sequence[RootChunk],
        leaves: Sequence[LeafChunk],
    ) -> None:
        async with self._database.session() as session:
            document = await session.scalar(
                select(DocumentModel)
                .where(DocumentModel.id == snapshot.document_id)
                .with_for_update()
            )
            version = await session.scalar(
                select(DocumentVersionModel)
                .where(DocumentVersionModel.id == snapshot.version_id)
                .with_for_update()
            )
            if (
                document is None
                or version is None
                or document.status != "processing"
                or version.status != "processing"
            ):
                raise AppError(ErrorCode.CONFLICT, "The LLM cleaning state changed unexpectedly.")
            repository = IngestionContentRepository(session)
            await repository.replace_content(
                version_id=snapshot.version_id,
                roots=roots,
                leaves=leaves,
                parser_provider=snapshot.parser_provider,
                parser_version=snapshot.parser_version,
            )
            actual = int(
                await session.scalar(
                    select(func.count())
                    .select_from(LeafModel)
                    .where(LeafModel.version_id == snapshot.version_id)
                )
                or 0
            )
            if actual != len(leaves):
                raise AppError(
                    ErrorCode.PROJECTION_COUNT_MISMATCH,
                    "PostgreSQL content count verification failed after LLM cleaning.",
                )
            version.status = "indexed"
            version.error_code = None
            version.error_message = None
            document.status = "ready"

    async def _restore(self, snapshot: _DocumentSnapshot) -> None:
        await self._vector_store.delete_by_version(snapshot.tenant_id, snapshot.version_id)
        await self._projection.project(self._projection_request(snapshot, snapshot.leaves))
        async with self._database.session() as session:
            document = await session.get(DocumentModel, snapshot.document_id)
            version = await session.get(DocumentVersionModel, snapshot.version_id)
            if document is None or version is None:
                raise RuntimeError("document disappeared during LLM cleaning recovery")
            document.status = "ready"
            version.status = "indexed"
            version.error_code = None
            version.error_message = None

    async def _mark_failed(self, snapshot: _DocumentSnapshot) -> None:
        async with self._database.session() as session:
            document = await session.get(DocumentModel, snapshot.document_id)
            version = await session.get(DocumentVersionModel, snapshot.version_id)
            if document is not None:
                document.status = "failed"
            if version is not None:
                version.status = "failed"
                version.error_code = ErrorCode.PROJECTION_CLEANUP_FAILED.value
                version.error_message = "The previous index could not be restored."

    @staticmethod
    def _projection_request(
        snapshot: _DocumentSnapshot, leaves: Sequence[LeafChunk]
    ) -> ProjectionRequest:
        return ProjectionRequest(
            snapshot.tenant_id,
            snapshot.collection_id,
            snapshot.document_id,
            snapshot.version_id,
            snapshot.roots[0].index_revision,
            tuple(leaves),
        )


def parse_cleaning_response(text: str, roots: Sequence[RootChunk]) -> tuple[str, ...]:
    """Parse strict JSON and reject structural or protected-fact changes."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise AppError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "The LLM cleaning response was not strict JSON.",
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"roots"}:
        raise _invalid_response("The LLM cleaning response has an invalid top-level shape.")
    values = payload["roots"]
    if not isinstance(values, list) or len(values) != len(roots):
        raise _invalid_response("The LLM cleaning response changed the Root count.")
    by_ordinal: dict[int, str] = {}
    for item in values:
        if not isinstance(item, dict) or set(item) != {"ordinal", "clean_text"}:
            raise _invalid_response("An LLM cleaning Root has an invalid shape.")
        ordinal = item["ordinal"]
        clean_text = item["clean_text"]
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not isinstance(clean_text, str)
            or not clean_text.strip()
            or ordinal in by_ordinal
        ):
            raise _invalid_response("An LLM cleaning Root has invalid values.")
        by_ordinal[ordinal] = clean_text
    expected = [root.ordinal for root in roots]
    if sorted(by_ordinal) != sorted(expected):
        raise _invalid_response("The LLM cleaning response changed Root ordinals.")
    cleaned = tuple(by_ordinal[ordinal] for ordinal in expected)
    repeated_edges = _repeated_edge_lines(roots)
    for root, value in zip(roots, cleaned, strict=True):
        _validate_anchors(root.clean_text, value, root.ordinal, repeated_edges)
    return cleaned


def _validate_anchors(
    before: str, after: str, ordinal: int, repeated_edges: Counter[str]
) -> None:
    if Counter(_FACT_ANCHOR.findall(before)) != Counter(_FACT_ANCHOR.findall(after)):
        raise _invalid_response(
            "The LLM cleaning response changed a protected fact anchor.", ordinal=ordinal
        )
    if Counter(_FENCE.findall(before)) != Counter(_FENCE.findall(after)):
        raise _invalid_response(
            "The LLM cleaning response changed a fenced code block.", ordinal=ordinal
        )
    if _structural_anchors(before) != _structural_anchors(after):
        raise _invalid_response(
            "The LLM cleaning response changed a protected heading or table header.",
            ordinal=ordinal,
        )
    _validate_lexical_content(before, after, ordinal, repeated_edges)


def _validate_lexical_content(
    before: str, after: str, ordinal: int, repeated_edges: Counter[str]
) -> None:
    before_tokens = Counter(token.casefold() for token in _LEXICAL_TOKEN.findall(before))
    after_tokens = Counter(token.casefold() for token in _LEXICAL_TOKEN.findall(after))
    if after_tokens - before_tokens:
        raise _invalid_response(
            "The LLM cleaning response added or rewrote lexical content.", ordinal=ordinal
        )
    before_sequence = [token.casefold() for token in _LEXICAL_TOKEN.findall(before)]
    actual_sequence = [token.casefold() for token in _LEXICAL_TOKEN.findall(after)]
    if actual_sequence == before_sequence:
        return
    removed_lines = Counter(_non_empty_lines(before)) - Counter(_non_empty_lines(after))
    removable = Counter(
        line for line, count in removed_lines.items() if repeated_edges[line] >= 2
    )
    if removable != removed_lines:
        raise _invalid_response(
            "The LLM cleaning response removed non-repeated lexical content.", ordinal=ordinal
        )
    expected = before_tokens.copy()
    for line, count in removable.items():
        removed_tokens = Counter(token.casefold() for token in _LEXICAL_TOKEN.findall(line))
        for token, token_count in removed_tokens.items():
            expected[token] -= token_count * count
    expected += Counter()
    remaining_lines = list(_non_empty_lines(before))
    for index in range(len(remaining_lines) - 1, -1, -1):
        line = remaining_lines[index]
        if removable[line] > 0:
            removable[line] -= 1
            remaining_lines.pop(index)
    expected_sequence = [
        token.casefold() for token in _LEXICAL_TOKEN.findall("\n".join(remaining_lines))
    ]
    if after_tokens != expected or actual_sequence != expected_sequence:
        raise _invalid_response(
            "The LLM cleaning response rewrote lexical content.", ordinal=ordinal
        )


def _non_empty_lines(text: str) -> tuple[str, ...]:
    return tuple(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _repeated_edge_lines(roots: Sequence[RootChunk]) -> Counter[str]:
    edges: Counter[str] = Counter()
    for root in roots:
        lines = _non_empty_lines(root.clean_text)
        if lines:
            edges[lines[0]] += 1
            edges[lines[-1]] += 1
    return edges


def _structural_anchors(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    anchors: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            anchors.append(line)
        if index + 1 < len(lines) and line.lstrip().startswith("|"):
            delimiter = lines[index + 1].strip()
            if delimiter.startswith("|") and "---" in delimiter:
                anchors.extend((line, lines[index + 1]))
    return tuple(anchors)


def _request_json(roots: Sequence[RootChunk]) -> str:
    return json.dumps(
        {
            "task": (
                "Clean presentation noise only. Preserve every fact, number, URL, email, "
                "quoted value, heading, table header, code block, Root count and ordinal. "
                "Preserve every lexical word and number; only remove an exact line when it is "
                "a repeated first/last edge across Roots."
            ),
            "roots": [{"ordinal": root.ordinal, "clean_text": root.clean_text} for root in roots],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


_SYSTEM_PROMPT = """You are a conservative document-cleaning function.
Return exactly one JSON object and no Markdown, commentary, or reasoning:
{"roots":[{"ordinal":0,"clean_text":"..."}]}
Keep every input Root exactly once with its original ordinal. Remove only obvious presentation
noise such as repeated whitespace or duplicated first/last edge lines. Never summarize, translate,
invent, paraphrase, reorder, or alter any lexical word, number, URL, email, quoted value, heading,
table header, or fenced code. Do not merge or split words. If uncertain, return the input clean_text
unchanged."""


def _root_from_model(model: RootModel) -> RootChunk:
    return RootChunk(
        model.id,
        model.tenant_id,
        model.document_id,
        model.version_id,
        model.index_revision,
        model.ordinal,
        RootKind(model.kind),
        model.source_locator,
        model.raw_text,
        model.clean_text,
        model.metadata_json,
        model.content_hash,
    )


def _leaf_from_model(model: LeafModel) -> LeafChunk:
    return LeafChunk(
        model.id,
        model.root_id,
        model.tenant_id,
        model.document_id,
        model.version_id,
        model.ordinal,
        model.text,
        model.retrieval_text,
        model.start_offset,
        model.end_offset,
        model.token_count,
        model.metadata_json,
        model.content_hash,
    )


def _fingerprint(roots: Sequence[RootChunk]) -> str:
    value = "\n".join(f"{root.id}:{root.content_hash}" for root in roots)
    return sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _invalid_response(message: str, *, ordinal: int | None = None) -> AppError:
    details: dict[str, object] = {}
    if ordinal is not None:
        details["root_ordinal"] = ordinal
    return AppError(ErrorCode.LLM_INVALID_RESPONSE, message, details)
