"""Async-safe correlation context shared by logs and traces."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ObservationContext:
    request_id: str | None = None
    tenant_id: str | None = None
    actor_id: str | None = None
    query_id: str | None = None
    job_id: str | None = None
    document_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            field.name: value
            for field in fields(self)
            if isinstance((value := getattr(self, field.name)), str)
        }


_CONTEXT: ContextVar[ObservationContext | None] = ContextVar(
    "enterprise_rag_observation_context", default=None
)


def current_context() -> ObservationContext:
    return _CONTEXT.get() or ObservationContext()


@contextmanager
def bind_context(**values: str | UUID | None) -> Iterator[ObservationContext]:
    unknown = set(values).difference(field.name for field in fields(ObservationContext))
    if unknown:
        raise ValueError(f"unknown observation context fields: {sorted(unknown)}")
    normalized = {
        name: str(value) if value is not None else None for name, value in values.items()
    }
    context = replace(current_context(), **normalized)
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def context_attributes() -> Mapping[str, str]:
    return {f"app.{name}": value for name, value in current_context().to_dict().items()}
