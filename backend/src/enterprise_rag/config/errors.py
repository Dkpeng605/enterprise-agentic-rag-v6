"""Stable configuration errors safe to report during startup."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SettingsErrorCode(StrEnum):
    """Machine-readable startup failure codes."""

    FILE_INVALID = "CONFIG_FILE_INVALID"
    PROVIDER_UNKNOWN = "CONFIG_PROVIDER_UNKNOWN"
    SECRET_MISSING = "CONFIG_SECRET_MISSING"
    VALUE_INVALID = "CONFIG_VALUE_INVALID"


@dataclass(frozen=True, slots=True)
class SettingsError(Exception):
    """A sanitized configuration error with a stable public shape."""

    code: SettingsErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
