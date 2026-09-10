"""Unified, client-safe application error model."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from enterprise_rag.domain.common import freeze_mapping, new_uuid7, require_non_empty, require_uuid7


class ErrorCode(StrEnum):
    CONFIG_FILE_INVALID = "CONFIG_FILE_INVALID"
    CONFIG_PROVIDER_UNKNOWN = "CONFIG_PROVIDER_UNKNOWN"
    CONFIG_SECRET_MISSING = "CONFIG_SECRET_MISSING"
    CONFIG_VALUE_INVALID = "CONFIG_VALUE_INVALID"
    PROVIDER_DUPLICATE = "PROVIDER_DUPLICATE"
    PROVIDER_UNKNOWN = "PROVIDER_UNKNOWN"
    PROVIDER_CAPABILITY_MISMATCH = "PROVIDER_CAPABILITY_MISMATCH"
    PROVIDER_REGISTRY_CLOSED = "PROVIDER_REGISTRY_CLOSED"
    PROVIDER_CLOSE_FAILED = "PROVIDER_CLOSE_FAILED"
    JOB_INVALID_TRANSITION = "JOB_INVALID_TRANSITION"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    DOCUMENT_UNSUPPORTED_TYPE = "DOCUMENT_UNSUPPORTED_TYPE"
    DOCUMENT_EMPTY = "DOCUMENT_EMPTY"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    code: ErrorCode
    message: str
    request_id: UUID
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_non_empty(self.message, "message")
        require_uuid7(self.request_id, "request_id")
        object.__setattr__(self, "details", freeze_mapping(self.details))

    def to_dict(self) -> dict[str, object]:
        from enterprise_rag.domain.common import to_json_value

        return {
            "code": self.code.value,
            "message": self.message,
            "request_id": str(self.request_id),
            "details": to_json_value(self.details),
        }


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    error: ErrorDetail

    def to_dict(self) -> dict[str, object]:
        return {"error": self.error.to_dict()}


@dataclass(frozen=True, slots=True)
class AppError(Exception):
    """Internal exception that exposes only its explicit sanitized fields."""

    code: ErrorCode
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    default_message: ClassVar[str] = "The application could not complete the request."

    def __post_init__(self) -> None:
        require_non_empty(self.message, "message")
        object.__setattr__(self, "details", freeze_mapping(self.details))

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_response(self, request_id: UUID | None = None) -> ErrorResponse:
        return ErrorResponse(
            ErrorDetail(
                code=self.code,
                message=self.message,
                request_id=request_id or new_uuid7(),
                details=self.details,
            )
        )
