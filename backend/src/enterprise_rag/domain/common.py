"""Pure standard-library primitives shared by domain models."""

import math
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class JsonSerializable(Protocol):
    def to_dict(self) -> dict[str, object]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_uuid7(*, timestamp_ms: int | None = None) -> UUID:
    """Generate an RFC 9562 UUIDv7 using only the Python 3.12 standard library."""

    milliseconds = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= milliseconds < 2**48:
        raise ValueError("UUIDv7 timestamp must fit in 48 bits")
    value = milliseconds << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return UUID(int=value)


def require_uuid7(value: UUID, field_name: str) -> None:
    if value.version != 7:
        raise ValueError(f"{field_name} must be UUIDv7")


def require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must use timezone-aware UTC")


def require_non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def stable_content_id(prefix: str, *parts: str) -> str:
    """Hash length-prefixed identity parts so reruns produce the same content ID."""

    if not prefix.isidentifier() or prefix.lower() != prefix:
        raise ValueError("content ID prefix must be a lowercase identifier")
    digest = sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefix}_{digest.hexdigest()}"


def freeze_json(value: object) -> object:
    """Copy a JSON-compatible value into an immutable representation."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("value must be a mapping")
    return frozen


def to_json_value(value: object) -> object:
    """Convert domain values to deterministic JSON-compatible Python values."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        require_utc(value, "datetime")
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, JsonSerializable):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported domain value type: {type(value).__name__}")
