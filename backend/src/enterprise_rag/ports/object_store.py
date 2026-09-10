"""Framework-neutral contract for immutable, content-addressed objects."""

import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from enterprise_rag.ports.provider import Provider

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KEY_PATTERN = re.compile(
    r"^sha256/(?P<first>[0-9a-f]{2})/(?P<second>[0-9a-f]{2})/(?P<digest>[0-9a-f]{64})$"
)


def validate_sha256(value: str) -> str:
    """Return a canonical SHA-256 digest or reject unsafe/non-canonical input."""

    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError("sha256 must contain exactly 64 lowercase hexadecimal characters")
    return value


def object_key_for_sha256(sha256: str) -> str:
    digest = validate_sha256(sha256)
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def validate_object_key(key: str) -> str:
    """Reject traversal, absolute paths, and non-canonical content keys."""

    match = _KEY_PATTERN.fullmatch(key)
    if match is None:
        raise ValueError("object key is not a canonical SHA-256 content key")
    digest = match.group("digest")
    if match.group("first") != digest[:2] or match.group("second") != digest[2:4]:
        raise ValueError("object key prefix does not match its SHA-256 digest")
    return key


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    created: bool

    def __post_init__(self) -> None:
        validate_object_key(self.key)
        validate_sha256(self.sha256)
        if self.key != object_key_for_sha256(self.sha256):
            raise ValueError("object key and sha256 do not match")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")


class ObjectStore(Provider, Protocol):
    async def put(
        self,
        chunks: AsyncIterable[bytes],
        *,
        expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredObject: ...

    def read(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool: ...

    async def list_keys(self) -> tuple[str, ...]: ...
