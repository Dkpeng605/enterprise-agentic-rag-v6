"""Configuration-specific names backed by the unified application error model."""

from enterprise_rag.domain.errors import AppError, ErrorCode


class SettingsError(AppError):
    """Configuration error retained as a catchable boundary-specific type."""


SettingsErrorCode = ErrorCode
