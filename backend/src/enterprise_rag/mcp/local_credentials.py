"""Local-only MCP secret persistence without logging or secret reuse."""

import os
import secrets
from pathlib import Path


def resolve_mcp_token_pepper(
    configured: str | None,
    *,
    path: Path,
    production: bool,
) -> str:
    """Return an explicit pepper or atomically create one for local development."""

    if configured is not None:
        value = configured.strip()
        if len(value.encode()) < 32:
            raise RuntimeError("MCP_TOKEN_PEPPER must contain at least 32 bytes")
        return value
    if production:
        raise RuntimeError("MCP_TOKEN_PEPPER is required in production")

    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_existing(path)
    try:
        os.write(descriptor, f"{generated}\n".encode())
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return generated


def _read_existing(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as stream:
            value = stream.read().strip()
    finally:
        os.close(descriptor)
    if len(value.encode()) < 32:
        raise RuntimeError("The local MCP token pepper file is invalid")
    return value
