"""Small OpenTelemetry boundary with safe attribute policy."""

from collections.abc import Awaitable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode, TracerProvider

from enterprise_rag import __version__
from enterprise_rag.observability.context import context_attributes

type AttributeValue = str | bool | int | float | tuple[str, ...]
_FORBIDDEN_ATTRIBUTE_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "api_key",
    "raw_token",
    "query.text",
    "document.text",
    "prompt",
    "request.body",
)
_TRACER_PROVIDER: ContextVar[TracerProvider | None] = ContextVar(
    "enterprise_rag_tracer_provider", default=None
)


def safe_attributes(values: Mapping[str, AttributeValue]) -> dict[str, AttributeValue]:
    sanitized: dict[str, AttributeValue] = {}
    for key, value in values.items():
        normalized = key.casefold()
        if any(part in normalized for part in _FORBIDDEN_ATTRIBUTE_PARTS):
            raise ValueError(f"sensitive span attribute is forbidden: {key}")
        sanitized[key] = value
    return sanitized


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Mapping[str, AttributeValue] | None = None,
    tracer_provider: TracerProvider | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    active_provider = tracer_provider or _TRACER_PROVIDER.get()
    provider_token = _TRACER_PROVIDER.set(active_provider) if active_provider else None
    tracer = trace.get_tracer(
        "enterprise_rag",
        __version__,
        tracer_provider=active_provider,
    )
    merged: dict[str, AttributeValue] = {"app.operation": name}
    merged.update(context_attributes())
    if attributes:
        merged.update(safe_attributes(attributes))
    try:
        with tracer.start_as_current_span(name, context=context, attributes=merged) as span:
            try:
                yield span
            except BaseException as error:
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
    finally:
        if provider_token is not None:
            _TRACER_PROVIDER.reset(provider_token)


async def trace_async[ResultT](
    name: str,
    operation: Awaitable[ResultT],
    *,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> ResultT:
    with start_span(name, attributes=attributes):
        return await operation


def trace_identifiers() -> tuple[str | None, str | None]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"
