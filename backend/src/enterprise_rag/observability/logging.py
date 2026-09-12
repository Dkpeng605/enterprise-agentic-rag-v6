"""One-line JSON application logs with bounded, correlated fields."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import TextIO

from enterprise_rag.observability.context import current_context
from enterprise_rag.observability.tracing import trace_identifiers

_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+"
)
_SAFE_EXTRAS = ("outcome", "duration_ms")


class JsonLogFormatter(logging.Formatter):
    def __init__(
        self,
        *,
        service: str = "enterprise-agentic-rag-v6",
        environment: str = "development",
    ) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = trace_identifiers()
        event_code = getattr(record, "event_code", None)
        context = current_context()
        error_code = getattr(record, "error_code", None)
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "logger": record.name,
            "event": event_code if isinstance(event_code, str) else record.name,
            "message": _redact(record.getMessage()),
            "request_id": context.request_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "tenant_id": context.tenant_id,
            "error_code": error_code if isinstance(error_code, str) else None,
        }
        for name in ("actor_id", "query_id", "job_id", "document_id"):
            value = getattr(context, name)
            if value is not None:
                payload[name] = value
        for name in _SAFE_EXTRAS:
            value = getattr(record, name, None)
            if isinstance(value, str | int | float | bool):
                payload[name] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def configure_json_logging(
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
    environment: str = "development",
) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonLogFormatter(environment=environment))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    return handler


def _redact(message: str) -> str:
    message = _BEARER.sub("Bearer [REDACTED]", message)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
