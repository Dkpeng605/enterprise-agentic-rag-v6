"""Stream and restore verified content-addressed ObjectStore archives."""

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, BinaryIO

OBJECT_KEY_PARTS = 4
OBJECT_DIGEST_LENGTH = 64
CHUNK_SIZE = 1024 * 1024


def _validate_object_key(key: str) -> None:
    parts = key.split("/")
    if (
        len(parts) != OBJECT_KEY_PARTS
        or parts[0] != "sha256"
        or len(parts[1]) != 2
        or len(parts[2]) != 2
        or len(parts[3]) != OBJECT_DIGEST_LENGTH
        or any(any(character not in "0123456789abcdef" for character in part) for part in parts[1:])
    ):
        raise ValueError(f"invalid ObjectStore key: {key}")


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _object_manifest(root: Path) -> list[dict[str, object]]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"ObjectStore root is not a directory: {root}")
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"ObjectStore contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"ObjectStore contains a non-file: {path}")
        key = path.relative_to(root).as_posix()
        _validate_object_key(key)
        size, digest = _digest_file(path)
        if digest != key.rsplit("/", 1)[-1]:
            raise ValueError(f"ObjectStore digest does not match key: {key}")
        entries.append({"key": key, "size": size, "sha256": digest})
    return entries


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def create_archive(root: Path, output: BinaryIO) -> int:
    entries = _object_manifest(root)
    manifest = json.dumps(
        {"schema_version": 1, "objects": entries},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with tarfile.open(fileobj=output, mode="w|gz") as archive:
        _add_bytes(archive, "manifest.json", manifest)
        resolved_root = root.resolve()
        for entry in entries:
            key = str(entry["key"])
            path = resolved_root / key
            info = tarfile.TarInfo(f"objects/{key}")
            size = entry["size"]
            if not isinstance(size, int):
                raise ValueError(f"ObjectStore manifest size is not an integer: {key}")
            info.size = size
            info.mode = 0o600
            info.mtime = 0
            with path.open("rb") as source:
                archive.addfile(info, source)
    return len(entries)


def _manifest(payload: bytes) -> dict[str, tuple[int, str]]:
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported ObjectStore archive manifest")
    objects = value.get("objects")
    if not isinstance(objects, list):
        raise ValueError("ObjectStore archive manifest has no object list")
    expected: dict[str, tuple[int, str]] = {}
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("ObjectStore archive manifest contains an invalid object")
        key = item.get("key")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            not isinstance(key, str)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != OBJECT_DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("ObjectStore archive manifest contains invalid metadata")
        _validate_object_key(key)
        if digest != key.rsplit("/", 1)[-1] or key in expected:
            raise ValueError("ObjectStore archive manifest contains a duplicate or mismatched key")
        expected[key] = (size, digest)
    return expected


def _safe_target(root: Path, key: str) -> Path:
    _validate_object_key(key)
    resolved_root = root.resolve()
    target = resolved_root / key
    if not target.resolve().is_relative_to(resolved_root):
        raise ValueError("ObjectStore archive escapes its root")
    cursor = resolved_root
    for part in key.split("/")[:-1]:
        cursor /= part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("ObjectStore restore encountered a symlinked directory")
    return target


def _restore_member(
    root: Path,
    member: tarfile.TarInfo,
    expected: tuple[int, str],
    source: IO[bytes],
) -> None:
    key = member.name.removeprefix("objects/")
    target = _safe_target(root, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("ObjectStore restore refuses symlink targets")
    digest = hashlib.sha256()
    size = 0
    temporary_name: str | None = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=".restore-", delete=False) as temporary:
            temporary_name = temporary.name
            while chunk := source.read(CHUNK_SIZE):
                temporary.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        expected_size, expected_digest = expected
        if (size, digest.hexdigest()) != (expected_size, expected_digest):
            raise ValueError(f"ObjectStore archive content does not match key: {key}")
        if target.exists():
            if not target.is_file() or _digest_file(target) != expected:
                raise ValueError(f"ObjectStore restore refuses to replace different content: {key}")
        else:
            os.link(temporary_name, target)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def restore_archive(root: Path, source: BinaryIO) -> int:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    expected: dict[str, tuple[int, str]] | None = None
    restored: set[str] = set()
    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            if member.name == "manifest.json":
                if expected is not None or not member.isfile():
                    raise ValueError("ObjectStore archive has an invalid manifest entry")
                manifest_source = archive.extractfile(member)
                if manifest_source is None:
                    raise ValueError("ObjectStore archive manifest is unreadable")
                expected = _manifest(manifest_source.read())
                continue
            if not member.name.startswith("objects/") or not member.isfile():
                raise ValueError("ObjectStore archive contains an unsupported entry")
            if expected is None:
                raise ValueError("ObjectStore archive must start with manifest.json")
            key = member.name.removeprefix("objects/")
            if key not in expected or key in restored:
                raise ValueError("ObjectStore archive contains an unknown or duplicate object")
            member_source = archive.extractfile(member)
            if member_source is None:
                raise ValueError("ObjectStore archive object is unreadable")
            _restore_member(root, member, expected[key], member_source)
            restored.add(key)
    if expected is None or restored != set(expected):
        raise ValueError("ObjectStore archive is incomplete")
    return len(restored)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--root", type=Path, default=Path("/data/runtime/object-store"))
    restore = commands.add_parser("restore")
    restore.add_argument("--root", type=Path, default=Path("/data/runtime/object-store"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            count = create_archive(arguments.root, sys.stdout.buffer)
        else:
            count = restore_archive(arguments.root, sys.stdin.buffer)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit(f"ObjectStore archive failed: {error}") from error
    if arguments.command == "restore":
        print(json.dumps({"restored_objects": count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
