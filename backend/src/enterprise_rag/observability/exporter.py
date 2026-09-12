"""Bounded in-memory OpenTelemetry exporter for async durable draining."""

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import isfinite
from threading import Lock

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from enterprise_rag.ports.traces import StoredSpan

_ALLOWED_ATTRIBUTE_PREFIXES = ("app.", "rag.", "provider.", "http.", "url.path")
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


class BufferedSpanExporter(SpanExporter):
    def __init__(self, *, max_traces: int = 1_000, max_spans_per_trace: int = 200) -> None:
        if max_traces <= 0 or max_spans_per_trace <= 0:
            raise ValueError("span buffer limits must be positive")
        self._max_traces = max_traces
        self._max_spans = max_spans_per_trace
        self._traces: OrderedDict[str, list[StoredSpan]] = OrderedDict()
        self._lock = Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for span in spans:
                try:
                    serialized = _stored_span(span)
                except (TypeError, ValueError):
                    continue
                if serialized is None:
                    continue
                bucket = self._traces.setdefault(serialized.trace_id, [])
                if len(bucket) < self._max_spans:
                    bucket.append(serialized)
                self._traces.move_to_end(serialized.trace_id)
                while len(self._traces) > self._max_traces:
                    self._traces.popitem(last=False)
        return SpanExportResult.SUCCESS

    def take(self, trace_id: str) -> tuple[StoredSpan, ...]:
        with self._lock:
            spans = self._traces.pop(trace_id, [])
        return tuple(sorted(spans, key=lambda item: (item.started_at, item.span_id)))

    def shutdown(self) -> None:
        with self._lock:
            self._traces.clear()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


def _stored_span(span: ReadableSpan) -> StoredSpan | None:
    context = span.context
    if context is None or span.start_time is None or span.end_time is None:
        return None
    parent_id = f"{span.parent.span_id:016x}" if span.parent is not None else None
    attributes = {
        key: _json_value(value)
        for key, value in (span.attributes or {}).items()
        if _allowed_attribute(key)
    }
    events = tuple(
        {
            "name": event.name,
            "timestamp": _time(event.timestamp).isoformat() if event.timestamp else None,
            "attributes": {
                key: _json_value(value)
                for key, value in (event.attributes or {}).items()
                if _allowed_event_attribute(key)
            },
        }
        for event in span.events
    )
    return StoredSpan(
        f"{context.trace_id:032x}",
        f"{context.span_id:016x}",
        parent_id,
        span.name,
        _time(span.start_time),
        _time(span.end_time),
        span.status.status_code.name,
        attributes,
        events,
    )


def _time(nanoseconds: int) -> datetime:
    return datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=UTC)


def _json_value(value: object) -> object:
    if isinstance(value, float):
        return value if isfinite(value) else "non_finite"
    if isinstance(value, str | bool | int) or value is None:
        return value
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _allowed_attribute(key: str) -> bool:
    normalized = key.casefold()
    return key.startswith(_ALLOWED_ATTRIBUTE_PREFIXES) and not any(
        part in normalized for part in _FORBIDDEN_ATTRIBUTE_PARTS
    )


def _allowed_event_attribute(key: str) -> bool:
    return _allowed_attribute(key) or key in {"error.type", "error.code"}
