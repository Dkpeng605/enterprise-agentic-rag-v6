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
    DOCUMENT_ENCRYPTED = "DOCUMENT_ENCRYPTED"
    DOCUMENT_CORRUPT = "DOCUMENT_CORRUPT"
    DOCUMENT_ENCODING_INVALID = "DOCUMENT_ENCODING_INVALID"
    OCR_LANGUAGE_MISSING = "OCR_LANGUAGE_MISSING"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_FAILED = "OCR_FAILED"
    EMBEDDING_INPUT_INVALID = "EMBEDDING_INPUT_INVALID"
    EMBEDDING_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
    EMBEDDING_INVALID_RESPONSE = "EMBEDDING_INVALID_RESPONSE"
    RERANKER_UNAVAILABLE = "RERANKER_UNAVAILABLE"
    RERANKER_INVALID_RESPONSE = "RERANKER_INVALID_RESPONSE"
    PROJECTION_UPSERT_FAILED = "PROJECTION_UPSERT_FAILED"
    PROJECTION_COUNT_MISMATCH = "PROJECTION_COUNT_MISMATCH"
    PROJECTION_CLEANUP_FAILED = "PROJECTION_CLEANUP_FAILED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    CSRF_INVALID = "CSRF_INVALID"
    FORBIDDEN = "FORBIDDEN"
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
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


class AppError(Exception):
    """Exception with read-only public fields and interpreter-managed traceback state."""

    __slots__ = ("_code", "_details", "_message")

    default_message: ClassVar[str] = "The application could not complete the request."

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        require_non_empty(message, "message")
        self._code = code
        self._message = message
        self._details = freeze_mapping(details or {})

    @property
    def code(self) -> ErrorCode:
        return self._code

    @property
    def message(self) -> str:
        return self._message

    @property
    def details(self) -> Mapping[str, object]:
        return self._details

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
