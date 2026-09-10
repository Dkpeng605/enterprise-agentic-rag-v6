"""Ingestion job state and immutable worker-facing snapshots."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from enterprise_rag.domain.common import require_utc, require_uuid7, to_json_value


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    id: UUID
    tenant_id: UUID
    document_id: UUID
    version_id: UUID
    type: str
    status: JobStatus
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_until: datetime | None
    heartbeat_at: datetime | None
    progress: int
    stage: str | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool

    def __post_init__(self) -> None:
        for name in ("id", "tenant_id", "document_id", "version_id"):
            require_uuid7(getattr(self, name), name)
        require_utc(self.available_at, "available_at")
        for name in ("lease_until", "heartbeat_at"):
            value = getattr(self, name)
            if value is not None:
                require_utc(value, name)
        if not 0 <= self.progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        if self.attempts < 0 or self.max_attempts <= 0:
            raise ValueError("attempt counts are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "tenant_id": str(self.tenant_id),
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "type": self.type,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "available_at": to_json_value(self.available_at),
            "lease_owner": self.lease_owner,
            "lease_until": to_json_value(self.lease_until),
            "heartbeat_at": to_json_value(self.heartbeat_at),
            "progress": self.progress,
            "stage": self.stage,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "cancel_requested": self.cancel_requested,
        }
