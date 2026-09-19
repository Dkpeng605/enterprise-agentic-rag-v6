"""Bounded, tenant-context query input/output capture for trace inspection."""

import json
import math
from collections.abc import Mapping, Sequence

from opentelemetry import trace

from enterprise_rag.observability.context import current_context

_MAX_JSON_CHARS = 262_144
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "raw_token",
        "secret",
        "set_cookie",
    }
)


def record_trace_io(component: str, direction: str, payload: object) -> None:
    """Attach one bounded JSON payload to the active query span.

    Query traces intentionally include user questions, evidence text, model prompts,
    and visible model responses. Credentials remain forbidden, and the helper is a
    no-op outside a bound query so ingestion does not persist whole document batches.
    """

    if direction not in {"input", "output"}:
        raise ValueError("trace I/O direction must be input or output")
    if not component.strip():
        raise ValueError("trace I/O component must not be blank")
    if current_context().query_id is None:
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    safe_payload = _json_safe(payload)
    serialized = json.dumps(
        safe_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    truncated = len(serialized) > _MAX_JSON_CHARS
    if truncated:
        serialized = json.dumps(
            {
                "truncated": True,
                "original_chars": len(serialized),
                "preview": serialized[:_MAX_JSON_CHARS],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    span.add_event(
        "rag.trace.io",
        {
            "rag.io.component": component,
            "rag.io.direction": direction,
            "rag.io.json": serialized,
            "rag.io.truncated": truncated,
        },
    )


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "non_finite"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            result[name] = "[REDACTED]" if name.casefold() in _SENSITIVE_KEYS else _json_safe(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    return str(value)
