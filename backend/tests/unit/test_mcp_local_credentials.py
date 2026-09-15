import stat
from pathlib import Path

import pytest

from enterprise_rag.mcp.local_credentials import resolve_mcp_token_pepper


def test_development_pepper_is_generated_once_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "mcp-token-pepper"

    first = resolve_mcp_token_pepper(None, path=path, production=False)
    second = resolve_mcp_token_pepper(None, path=path, production=False)

    assert first == second
    assert len(first.encode()) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8").strip() == first


def test_explicit_pepper_does_not_create_a_file(tmp_path: Path) -> None:
    path = tmp_path / "mcp-token-pepper"
    value = "explicit-mcp-pepper-with-at-least-32-bytes"

    assert resolve_mcp_token_pepper(value, path=path, production=True) == value
    assert not path.exists()


def test_production_never_generates_a_missing_pepper(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="MCP_TOKEN_PEPPER"):
        resolve_mcp_token_pepper(
            None, path=tmp_path / "mcp-token-pepper", production=True
        )
