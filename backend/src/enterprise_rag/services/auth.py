"""Server-side anonymous demo sessions and their fixed tenant boundary."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import (
    AnonymousSessionModel,
    CollectionModel,
    MembershipModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.domain.common import new_uuid7, require_utc
from enterprise_rag.domain.errors import AppError, ErrorCode

DEMO_TENANT_ID = UUID("01900000-0000-7000-8000-00000000d001")
DEMO_ACTOR_ID = UUID("01900000-0000-7000-8000-00000000d002")
DEMO_COLLECTION_ID = UUID("01900000-0000-7000-8000-00000000d003")
SESSION_COOKIE = "rag_session"


@dataclass(frozen=True, slots=True)
class Principal:
    session_id: UUID
    tenant_id: UUID
    actor_id: UUID
    actor_type: str = "anonymous"
    role: str = "demo_operator"


@dataclass(frozen=True, slots=True)
class SessionGrant:
    principal: Principal
    token: str
    csrf_token: str
    expires_at: datetime
    created: bool


class AnonymousSessionService:
    def __init__(
        self,
        database: Database,
        *,
        secret: str,
        tenant_slug: str = "demo",
        session_minutes: int = 480,
        enabled: bool = True,
    ) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("session secret must contain at least 32 bytes")
        if session_minutes <= 0:
            raise ValueError("session_minutes must be positive")
        self._database = database
        self._secret = secret.encode()
        self._tenant_slug = tenant_slug
        self._duration = timedelta(minutes=session_minutes)
        self._enabled = enabled

    async def get_or_create(self, token: str | None, *, now: datetime) -> SessionGrant:
        require_utc(now, "now")
        self._require_enabled()
        if token:
            existing = await self._lookup(token, now=now)
            if existing is not None:
                csrf_token = secrets.token_urlsafe(32)
                expires_at = now + self._duration
                async with self._database.session() as session:
                    model = await session.get(AnonymousSessionModel, existing.session_id)
                    if model is None:
                        raise self._authentication_error()
                    model.csrf_hash = self._hash(csrf_token)
                    model.expires_at = expires_at
                return SessionGrant(existing, token, csrf_token, expires_at, False)

        tenant_id, actor_id = await self._ensure_demo_workspace()
        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + self._duration
        model = AnonymousSessionModel(
            id=new_uuid7(),
            token_hash=self._hash(raw_token),
            csrf_hash=self._hash(csrf_token),
            tenant_id=tenant_id,
            actor_id=actor_id,
            expires_at=expires_at,
        )
        async with self._database.session() as session:
            session.add(model)
        principal = Principal(model.id, tenant_id, actor_id)
        return SessionGrant(principal, raw_token, csrf_token, expires_at, True)

    async def authenticate(self, token: str | None, *, now: datetime) -> Principal:
        require_utc(now, "now")
        self._require_enabled()
        if not token:
            raise self._authentication_error()
        principal = await self._lookup(token, now=now)
        if principal is None:
            raise self._authentication_error()
        return principal

    async def require_csrf(
        self, token: str | None, csrf_token: str | None, *, now: datetime
    ) -> Principal:
        principal = await self.authenticate(token, now=now)
        if not csrf_token:
            raise AppError(ErrorCode.CSRF_INVALID, "A valid CSRF token is required.")
        async with self._database.session() as session:
            stored_hash = await session.scalar(
                select(AnonymousSessionModel.csrf_hash).where(
                    AnonymousSessionModel.id == principal.session_id
                )
            )
        if stored_hash is None or not hmac.compare_digest(stored_hash, self._hash(csrf_token)):
            raise AppError(ErrorCode.CSRF_INVALID, "A valid CSRF token is required.")
        return principal

    async def revoke(self, token: str | None, *, now: datetime) -> None:
        principal = await self.authenticate(token, now=now)
        async with self._database.session() as session:
            model = await session.get(AnonymousSessionModel, principal.session_id)
            if model is not None:
                model.revoked_at = now

    async def _lookup(self, token: str, *, now: datetime) -> Principal | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(AnonymousSessionModel, TenantModel, UserModel)
                    .join(TenantModel, TenantModel.id == AnonymousSessionModel.tenant_id)
                    .join(UserModel, UserModel.id == AnonymousSessionModel.actor_id)
                    .where(
                        AnonymousSessionModel.token_hash == self._hash(token),
                        AnonymousSessionModel.revoked_at.is_(None),
                        AnonymousSessionModel.expires_at > now,
                        TenantModel.status == "active",
                        UserModel.status == "active",
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        model, _, _ = row
        return Principal(model.id, model.tenant_id, model.actor_id)

    async def _ensure_demo_workspace(self) -> tuple[UUID, UUID]:
        async with self._database.session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"anonymous-demo:{self._tenant_slug}"},
            )
            tenant = await session.scalar(
                select(TenantModel).where(TenantModel.slug == self._tenant_slug)
            )
            if tenant is None:
                tenant = TenantModel(
                    id=DEMO_TENANT_ID,
                    name="Enterprise RAG 匿名演示",
                    slug=self._tenant_slug,
                )
                session.add(tenant)
                await session.flush()
            actor = await session.scalar(
                select(UserModel).where(UserModel.email == "demo-operator@example.invalid")
            )
            if actor is None:
                actor = UserModel(
                    id=DEMO_ACTOR_ID,
                    email="demo-operator@example.invalid",
                    password_hash="anonymous-session-only",
                )
                session.add(actor)
                await session.flush()
            membership = await session.get(MembershipModel, (tenant.id, actor.id))
            if membership is None:
                session.add(
                    MembershipModel(tenant_id=tenant.id, user_id=actor.id, role="tenant_admin")
                )
            seed = await session.scalar(
                select(CollectionModel).where(
                    CollectionModel.tenant_id == tenant.id,
                    CollectionModel.is_seed.is_(True),
                )
            )
            if seed is None:
                session.add(
                    CollectionModel(
                        id=DEMO_COLLECTION_ID,
                        tenant_id=tenant.id,
                        name="示例知识库",
                        description="可重置的匿名演示集合",
                        visibility="tenant",
                        is_seed=True,
                    )
                )
            return tenant.id, actor.id

    def _hash(self, value: str) -> str:
        return hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise self._authentication_error()

    @staticmethod
    def _authentication_error() -> AppError:
        return AppError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            "An active session is required.",
        )
