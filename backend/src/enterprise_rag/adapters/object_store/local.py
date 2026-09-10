"""Crash-safe local content-addressed object storage."""

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from enterprise_rag.ports.object_store import (
    StoredObject,
    object_key_for_sha256,
    validate_object_key,
    validate_sha256,
)
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind


class LocalObjectStore:
    """Store immutable bytes under a digest-derived key inside one owned root."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._objects_root = self._root / "objects"
        self._temporary_root = self._root / "temporary"
        self._objects_root.mkdir(parents=True, exist_ok=True)
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        self._mutation_lock = asyncio.Lock()

    @asynccontextmanager
    async def mutation_guard(self) -> AsyncIterator[None]:
        """Serialize reference-changing operations for the single-process local adapter."""

        async with self._mutation_lock:
            yield

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.OBJECT_STORE,
            name="local",
            version="1",
            capabilities=frozenset({"streaming_write", "content_addressed", "atomic_publish"}),
            is_remote=False,
            health=ProviderHealth.HEALTHY,
        )

    async def aclose(self) -> None:
        return None

    async def put(
        self,
        chunks: AsyncIterable[bytes],
        *,
        expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredObject:
        if expected_sha256 is not None:
            validate_sha256(expected_sha256)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix="upload-", suffix=".part", dir=self._temporary_root
        )
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("object chunks must be bytes")
                    size_bytes += len(chunk)
                    if max_bytes is not None and size_bytes > max_bytes:
                        raise ValueError("object exceeds max_bytes")
                    digest.update(chunk)
                    await asyncio.to_thread(stream.write, chunk)
                await asyncio.to_thread(stream.flush)
                await asyncio.to_thread(os.fsync, stream.fileno())

            sha256 = digest.hexdigest()
            if expected_sha256 is not None and sha256 != expected_sha256:
                raise ValueError("object SHA-256 does not match expected_sha256")

            key = object_key_for_sha256(sha256)
            destination = self._path_for_key(key)
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            created = await asyncio.to_thread(
                self._publish_once, temporary_path, destination, sha256, size_bytes
            )
            return StoredObject(
                key=key,
                sha256=sha256,
                size_bytes=size_bytes,
                created=created,
            )
        finally:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)

    async def read(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        path = self._path_for_key(key)
        stream = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(stream.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(stream.close)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path_for_key(key).is_file)

    async def delete(self, key: str) -> bool:
        path = self._path_for_key(key)
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            return False
        return True

    async def list_keys(self) -> tuple[str, ...]:
        def scan() -> tuple[str, ...]:
            keys: list[str] = []
            if not self._objects_root.exists():
                return ()
            for path in self._objects_root.rglob("*"):
                if not path.is_file():
                    continue
                key = path.relative_to(self._objects_root).as_posix()
                try:
                    validate_object_key(key)
                except ValueError:
                    continue
                keys.append(key)
            return tuple(sorted(keys))

        return await asyncio.to_thread(scan)

    def _path_for_key(self, key: str) -> Path:
        canonical_key = validate_object_key(key)
        candidate = self._objects_root.joinpath(*canonical_key.split("/"))
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._objects_root.resolve()):
            raise ValueError("object key resolves outside the object-store root")
        return resolved

    @staticmethod
    def _publish_once(
        temporary_path: Path, destination: Path, sha256: str, size_bytes: int
    ) -> bool:
        """Atomically make the complete file visible without replacing an existing digest."""

        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            if destination.stat().st_size != size_bytes:
                raise OSError(
                    "existing content-addressed object has an unexpected size"
                ) from None
            existing_digest = hashlib.sha256()
            with destination.open("rb") as existing:
                while block := existing.read(1024 * 1024):
                    existing_digest.update(block)
            if existing_digest.hexdigest() != sha256:
                raise OSError(
                    "existing content-addressed object failed SHA-256 verification"
                ) from None
            return False
        return True
