"""Stable index-contract fingerprint shared by API and ingestion Workers."""

import hashlib


def index_revision(
    embedding_model: str,
    *,
    sparse_provider: str,
    sparse_version: str,
    prefix: str = "semantic",
) -> str:
    """Return the revision key for one dense/sparse index contract."""

    if not prefix.strip():
        raise ValueError("index revision prefix must not be blank")
    contract = f"{embedding_model}\0{sparse_provider}\0{sparse_version}"
    digest = hashlib.sha256(contract.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


__all__ = ["index_revision"]
