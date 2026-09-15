"""Trusted operator service for opaque MCP bearer-token lifecycle."""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.models import (
    ApiTokenModel,
    CollectionModel,
    MembershipModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.domain.common import new_uuid7, require_utc, utc_now
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.mcp.access import ALL_MCP_SCOPES, MCP_ACCESS_SCOPE


@dataclass(frozen=True, slots=True)
class IssuedMcpToken:
    id: UUID
    name: str
    token_prefix: str
    scopes: tuple[str, ...]
    collection_ids: tuple[UUID, ...]
    expires_at: datetime
    raw_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpTokenSummary:
    id: UUID
    name: str
    token_prefix: str
    scopes: tuple[str, ...]
    collection_ids: tuple[UUID, ...]
    expires_at: datetime
    revoked_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "name": self.name,
            "token_prefix": self.token_prefix,
            "scopes": list(self.scopes),
            "collection_ids": [str(value) for value in self.collection_ids],
            "expires_at": self.expires_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


class McpTokenOperator:
    """Issue and revoke tokens from a trusted local process, never an anonymous API."""

    def __init__(
        self,
        database: Database,
        *,
        pepper: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if len(pepper.encode()) < 32:
            raise ValueError("MCP token pepper must contain at least 32 bytes")
        self._database = database
        self._pepper = pepper.encode()
        self._clock = clock

    async def issue(
        self,
        *,
        tenant_slug: str,
        actor_email: str,
        name: str,
        collection_ids: tuple[UUID, ...] | None,
        scopes: tuple[str, ...],
        expires_in: timedelta,
    ) -> IssuedMcpToken:
        _validate_issue(tenant_slug, actor_email, name, scopes, expires_in)
        now = self._clock()
        require_utc(now, "now")
        expires_at = now + expires_in
        raw_token = f"rag_mcp_{secrets.token_urlsafe(36)}"
        token_id = new_uuid7()
        token_hash = hmac.new(
            self._pepper, raw_token.encode(), hashlib.sha256
        ).hexdigest()
        async with self._database.session() as session:
            tenant = await session.scalar(
                select(TenantModel).where(
                    TenantModel.slug == tenant_slug,
                    TenantModel.status == "active",
                )
            )
            if tenant is None:
                raise AppError(ErrorCode.NOT_FOUND, "The requested tenant is unavailable.")
            actor = await session.scalar(
                select(UserModel)
                .join(
                    MembershipModel,
                    MembershipModel.user_id == UserModel.id,
                )
                .where(
                    MembershipModel.tenant_id == tenant.id,
                    func.lower(UserModel.email) == actor_email.casefold(),
                    UserModel.status == "active",
                )
            )
            if actor is None:
                raise AppError(ErrorCode.NOT_FOUND, "The requested actor is unavailable.")
            available = tuple(
                await session.scalars(
                    select(CollectionModel.id)
                    .where(
                        CollectionModel.tenant_id == tenant.id,
                        CollectionModel.status == "active",
                    )
                    .order_by(CollectionModel.created_at, CollectionModel.id)
                )
            )
            selected = available if collection_ids is None else collection_ids
            if (
                not selected
                or len(selected) != len(set(selected))
                or not set(selected).issubset(available)
            ):
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "MCP tokens require active collections from the selected tenant.",
                )
            session.add(
                ApiTokenModel(
                    id=token_id,
                    tenant_id=tenant.id,
                    actor_id=actor.id,
                    name=name.strip(),
                    token_prefix=raw_token[:12],
                    token_hash=token_hash,
                    scopes=list(scopes),
                    collection_ids=[str(value) for value in selected],
                    expires_at=expires_at,
                )
            )
        return IssuedMcpToken(
            token_id,
            name.strip(),
            raw_token[:12],
            scopes,
            selected,
            expires_at,
            raw_token,
        )

    async def list(self, *, tenant_slug: str) -> tuple[McpTokenSummary, ...]:
        async with self._database.session() as session:
            rows = tuple(
                await session.scalars(
                    select(ApiTokenModel)
                    .join(TenantModel, TenantModel.id == ApiTokenModel.tenant_id)
                    .where(TenantModel.slug == tenant_slug)
                    .order_by(ApiTokenModel.created_at.desc(), ApiTokenModel.id.desc())
                )
            )
        return tuple(_summary(item) for item in rows)

    async def revoke(self, *, tenant_slug: str, token_id: UUID) -> bool:
        now = self._clock()
        require_utc(now, "now")
        async with self._database.session() as session:
            model = await session.scalar(
                select(ApiTokenModel)
                .join(TenantModel, TenantModel.id == ApiTokenModel.tenant_id)
                .where(
                    TenantModel.slug == tenant_slug,
                    ApiTokenModel.id == token_id,
                )
                .with_for_update()
            )
            if model is None:
                raise AppError(ErrorCode.NOT_FOUND, "The requested MCP token is unavailable.")
            if model.revoked_at is not None:
                return False
            model.revoked_at = now
            return True


def _validate_issue(
    tenant_slug: str,
    actor_email: str,
    name: str,
    scopes: Sequence[str],
    expires_in: timedelta,
) -> None:
    if not tenant_slug.strip() or not actor_email.strip():
        raise AppError(ErrorCode.VALIDATION_ERROR, "Tenant and actor are required.")
    if not name.strip() or len(name.strip()) > 200:
        raise AppError(ErrorCode.VALIDATION_ERROR, "The MCP token name is invalid.")
    if (
        MCP_ACCESS_SCOPE not in scopes
        or len(scopes) != len(set(scopes))
        or not set(scopes).issubset(ALL_MCP_SCOPES)
    ):
        raise AppError(ErrorCode.VALIDATION_ERROR, "The MCP token scopes are invalid.")
    if not timedelta(minutes=1) <= expires_in <= timedelta(days=365):
        raise AppError(ErrorCode.VALIDATION_ERROR, "The MCP token expiry is invalid.")


def _summary(model: ApiTokenModel) -> McpTokenSummary:
    try:
        scopes = tuple(str(value) for value in model.scopes)
        collection_ids = tuple(UUID(str(value)) for value in model.collection_ids)
    except (TypeError, ValueError) as error:
        raise AppError(ErrorCode.INTERNAL_ERROR, "Stored MCP token metadata is invalid.") from error
    return McpTokenSummary(
        model.id,
        model.name,
        model.token_prefix,
        scopes,
        collection_ids,
        model.expires_at,
        model.revoked_at,
    )
