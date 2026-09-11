"""Explicit HTTP request and response schemas for the workspace API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from enterprise_rag.domain.documents import DocumentVisibility


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ErrorDetailModel(ApiModel):
    code: str
    message: str
    request_id: UUID
    details: dict[str, object]


class ErrorResponseModel(ApiModel):
    error: ErrorDetailModel


class TenantModel(ApiModel):
    id: UUID
    slug: str


class AuthMeResponse(ApiModel):
    actor_type: Literal["anonymous"]
    role: Literal["demo_operator"]
    tenant: TenantModel
    permissions: list[str]
    csrf_token: str
    expires_at: datetime


class CollectionCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    visibility: DocumentVisibility = DocumentVisibility.TENANT


class CollectionPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    visibility: DocumentVisibility | None = None


class CollectionDelete(ApiModel):
    confirm_name: str = Field(min_length=1, max_length=200)


class CollectionResponse(ApiModel):
    id: UUID
    name: str
    description: str | None
    visibility: str
    is_seed: bool
    document_count: int
    ready_document_count: int
    created_at: datetime
    updated_at: datetime


class CollectionListResponse(ApiModel):
    items: list[CollectionResponse]


class CollectionDeleteResponse(ApiModel):
    id: UUID
    status: str
    job_ids: list[UUID]


class UploadResponse(ApiModel):
    document_id: UUID
    version_id: UUID
    job_id: UUID
    deduplicated: bool
    status: str


class DocumentResponse(ApiModel):
    id: UUID
    collection_id: UUID
    title: str
    organization: str | None
    status: str
    visibility: str
    version_id: UUID
    source_name: str
    media_type: str
    size_bytes: int
    sha256_prefix: str
    created_at: datetime
    updated_at: datetime


class JobResponse(ApiModel):
    id: UUID
    document_id: UUID
    version_id: UUID
    type: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    heartbeat_at: datetime | None
    progress: int
    stage: str | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool


class DocumentDetailResponse(DocumentResponse):
    root_count: int
    leaf_count: int
    recent_job: JobResponse | None
    version_error_code: str | None
    version_error_message: str | None


class DocumentListResponse(ApiModel):
    items: list[DocumentResponse]
    next_cursor: str | None


class DocumentDeleteResponse(ApiModel):
    document_id: UUID
    job_id: UUID
    status: str
    already_requested: bool
