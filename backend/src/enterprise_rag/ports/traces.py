"""Transport-neutral records and persistence contracts for completed traces."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import (
    freeze_mapping,
    require_non_empty,
    require_utc,
    require_uuid7,
)

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True, slots=True)
class StoredSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at: datetime
    finished_at: datetime
    status: str
    attributes: Mapping[str, object] = field(default_factory=dict)
    events: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not _TRACE_ID.fullmatch(self.trace_id) or not _SPAN_ID.fullmatch(self.span_id):
            raise ValueError("trace/span ID has an invalid format")
        if self.parent_span_id is not None and not _SPAN_ID.fullmatch(self.parent_span_id):
            raise ValueError("parent span ID has an invalid format")
        require_non_empty(self.name, "span name")
        require_utc(self.started_at, "started_at")
        require_utc(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("span finish precedes start")
        object.__setattr__(self, "attributes", freeze_mapping(self.attributes))
        object.__setattr__(
            self,
            "events",
            tuple(MappingProxyType(dict(event)) for event in self.events),
        )

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1_000


@dataclass(frozen=True, slots=True)
class TraceCompletion:
    trace_id: str
    trace_type: str
    tenant_id: UUID
    actor_id: UUID | None
    actor_type: str
    subject_id: UUID
    request_id: UUID | None
    mode: str | None
    status: str
    started_at: datetime
    finished_at: datetime
    usage: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _TRACE_ID.fullmatch(self.trace_id):
            raise ValueError("trace ID has an invalid format")
        if self.trace_type not in {"query", "ingestion", "evaluation"}:
            raise ValueError("trace_type is invalid")
        require_uuid7(self.tenant_id, "tenant_id")
        require_uuid7(self.subject_id, "subject_id")
        for name in ("actor_id", "request_id"):
            value = getattr(self, name)
            if value is not None:
                require_uuid7(value, name)
        require_non_empty(self.actor_type, "actor_type")
        require_non_empty(self.status, "status")
        require_utc(self.started_at, "started_at")
        require_utc(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("trace finish precedes start")
        object.__setattr__(self, "usage", freeze_mapping(self.usage))
        object.__setattr__(self, "attributes", freeze_mapping(self.attributes))

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1_000


@dataclass(frozen=True, slots=True)
class TraceSummary:
    trace_id: str
    trace_type: str
    subject_id: UUID
    mode: str | None
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    span_count: int
    degraded: bool


@dataclass(frozen=True, slots=True)
class TracePage:
    items: tuple[TraceSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TraceDetail:
    summary: TraceSummary
    actor_type: str
    request_id: UUID | None
    usage: Mapping[str, object]
    attributes: Mapping[str, object]
    spans: tuple[StoredSpan, ...]


class TraceStore(Protocol):
    async def persist(
        self, completion: TraceCompletion, spans: tuple[StoredSpan, ...]
    ) -> None: ...

    async def list(
        self,
        tenant_id: UUID,
        *,
        trace_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> TracePage: ...

    async def get(self, tenant_id: UUID, trace_id: str) -> TraceDetail | None: ...


class TraceRecorder(Protocol):
    async def record(self, completion: TraceCompletion) -> None: ...
