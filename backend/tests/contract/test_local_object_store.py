import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.ports import object_key_for_sha256, validate_object_key


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


async def read_all(store: LocalObjectStore, key: str, *, chunk_size: int = 2) -> bytes:
    return b"".join([chunk async for chunk in store.read(key, chunk_size=chunk_size)])


@pytest.mark.anyio
async def test_streams_hashes_reads_lists_and_deletes_an_object(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "store")
    payload = b"content-addressed-object"
    sha256 = hashlib.sha256(payload).hexdigest()

    result = await store.put(chunks(b"content-", b"addressed-", b"object"))

    assert result.key == object_key_for_sha256(sha256)
    assert result.sha256 == sha256
    assert result.size_bytes == len(payload)
    assert result.created is True
    assert await store.exists(result.key)
    assert await read_all(store, result.key) == payload
    assert await store.list_keys() == (result.key,)
    assert await store.delete(result.key) is True
    assert await store.delete(result.key) is False
    assert not await store.exists(result.key)


@pytest.mark.anyio
async def test_duplicate_content_reuses_the_visible_object(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "store")

    first = await store.put(chunks(b"same", b" bytes"))
    visible_path = tmp_path / "store" / "objects" / first.key
    first_inode = visible_path.stat().st_ino
    second = await store.put(chunks(b"same bytes"), expected_sha256=first.sha256)

    assert second.key == first.key
    assert second.created is False
    assert visible_path.stat().st_ino == first_inode
    assert await read_all(store, first.key) == b"same bytes"
    assert list((tmp_path / "store" / "temporary").iterdir()) == []


@pytest.mark.anyio
async def test_concurrent_duplicate_publish_creates_exactly_one_object(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "store")

    results = await asyncio.gather(
        *(store.put(chunks(b"concurrent-content")) for _ in range(8))
    )

    assert sum(result.created for result in results) == 1
    assert len({result.key for result in results}) == 1
    assert await store.list_keys() == (results[0].key,)
    assert list((tmp_path / "store" / "temporary").iterdir()) == []


@pytest.mark.anyio
async def test_interrupted_or_rejected_writes_leave_no_visible_or_temporary_file(
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path / "store")

    async def interrupted() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("source disconnected")

    with pytest.raises(RuntimeError, match="disconnected"):
        await store.put(interrupted())
    with pytest.raises(ValueError, match="max_bytes"):
        await store.put(chunks(b"too-large"), max_bytes=3)
    with pytest.raises(ValueError, match="does not match"):
        await store.put(chunks(b"wrong"), expected_sha256="0" * 64)

    assert await store.list_keys() == ()
    assert list((tmp_path / "store" / "temporary").iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "unsafe_key",
    (
        "../secret",
        "/etc/passwd",
        "sha256/aa/bb/not-a-digest",
        f"sha256/ff/00/{'0' * 64}",
    ),
)
async def test_rejects_path_traversal_and_noncanonical_keys(
    tmp_path: Path, unsafe_key: str
) -> None:
    store = LocalObjectStore(tmp_path / "store")

    with pytest.raises(ValueError, match="object key"):
        validate_object_key(unsafe_key)
    with pytest.raises(ValueError, match="object key"):
        await store.exists(unsafe_key)


@pytest.mark.anyio
async def test_symlink_cannot_escape_the_owned_root(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    store = LocalObjectStore(store_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store_root / "objects" / "sha256").symlink_to(outside, target_is_directory=True)
    key = object_key_for_sha256(hashlib.sha256(b"x").hexdigest())

    with pytest.raises(ValueError, match="outside"):
        await store.exists(key)


def test_object_key_validation_rejects_uppercase_digest_and_mismatched_prefix() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        object_key_for_sha256("A" * 64)
    with pytest.raises(ValueError, match="prefix"):
        validate_object_key(f"sha256/ff/00/{'0' * 64}")
