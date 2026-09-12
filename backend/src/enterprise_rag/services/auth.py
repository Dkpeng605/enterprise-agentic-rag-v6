"""Cookie sessions for the anonymous demo and bootstrap system administrator."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select, text

from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.models import (
    AnonymousSessionModel,
    AuthenticatedSessionModel,
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
ADMIN_TENANT_ID = UUID("01900000-0000-7000-8000-00000000a001")
SESSION_COOKIE = "rag_session"
ANONYMOUS_PERMISSIONS = (
    "collections:manage",
    "documents:manage",
    "ingestion:read",
    "query:execute",
    "traces:read",
    "evaluations:run",
)
SYSTEM_PERMISSIONS = (
    "system:read",
    "providers:manage",
    "tenants:manage",
    "users:manage",
    "audit:read",
)
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$lkEUfKVrx8BCKC/i2lRcCA$"
    "0k3MYNvaZAoh8/3FK56HWFxXF5WoLJD0rtoDqZ7V+00"
)


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
    tenant_slug: str = "demo"
    permissions: tuple[str, ...] = ANONYMOUS_PERMISSIONS
    email: str | None = None


class AnonymousSessionService:
    """Manage anonymous and authenticated cookie sessions.

    The historical class name remains compatible with existing composition code.
    Authenticated administrator sessions are stored in a separate table and never
    inherit the anonymous demo role.
    """

    def __init__(
        self,
        database: Database,
        *,
        secret: str,
        tenant_slug: str = "demo",
        session_minutes: int = 480,
        enabled: bool = True,
        admin_email: str | None = None,
        admin_password: str | None = None,
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
        self._admin_email = admin_email.strip().casefold() if admin_email else None
        self._admin_password = admin_password
        self._passwords = PasswordHasher()

    async def get_or_create(self, token: str | None, *, now: datetime) -> SessionGrant:
        require_utc(now, "now")
        if token:
            authenticated = await self._lookup_authenticated(token, now=now)
            if authenticated is not None:
                return await self._rotate_authenticated(authenticated, token=token, now=now)
        self._require_enabled()
        if token:
            existing = await self._lookup_anonymous(token, now=now)
            if existing is not None:
                csrf_token = secrets.token_urlsafe(32)
                expires_at = now + self._duration
                async with self._database.session() as session:
                    model = await session.get(AnonymousSessionModel, existing.session_id)
                    if model is None:
                        raise self._authentication_error()
                    model.csrf_hash = self._hash(csrf_token)
                    model.expires_at = expires_at
                return SessionGrant(
                    existing,
                    token,
                    csrf_token,
                    expires_at,
                    False,
                    tenant_slug=self._tenant_slug,
                )

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
        return SessionGrant(
            principal,
            raw_token,
            csrf_token,
            expires_at,
            True,
            tenant_slug=self._tenant_slug,
        )

    async def login(self, email: str, password: str, *, now: datetime) -> SessionGrant:
        require_utc(now, "now")
        await self._bootstrap_admin()
        normalized = email.strip().casefold()
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(UserModel, TenantModel)
                    .join(MembershipModel, MembershipModel.user_id == UserModel.id)
                    .join(TenantModel, TenantModel.id == MembershipModel.tenant_id)
                    .where(
                        func.lower(UserModel.email) == normalized,
                        UserModel.status == "active",
                        UserModel.system_role.is_not(None),
                        TenantModel.status == "active",
                    )
                    .order_by(TenantModel.created_at)
                    .limit(1)
                )
            ).one_or_none()
        candidate_hash = row[0].password_hash if row is not None else DUMMY_PASSWORD_HASH
        valid = self._verify_password(candidate_hash, password)
        if row is None or not valid:
            raise self._authentication_error()
        user, tenant = row
        if user.system_role is None:
            raise self._authentication_error()
        if self._passwords.check_needs_rehash(user.password_hash):
            async with self._database.session() as session:
                stored = await session.get(UserModel, user.id)
                if stored is not None:
                    stored.password_hash = self._passwords.hash(password)
        return await self._create_authenticated(user, tenant, now=now)

    async def authenticate(self, token: str | None, *, now: datetime) -> Principal:
        require_utc(now, "now")
        if not token:
            raise self._authentication_error()
        authenticated = await self._lookup_authenticated(token, now=now)
        if authenticated is not None:
            return authenticated.principal
        self._require_enabled()
        principal = await self._lookup_anonymous(token, now=now)
        if principal is None:
            raise self._authentication_error()
        return principal

    async def require_csrf(
        self, token: str | None, csrf_token: str | None, *, now: datetime
    ) -> Principal:
        principal = await self.authenticate(token, now=now)
        if not csrf_token:
            raise AppError(ErrorCode.CSRF_INVALID, "A valid CSRF token is required.")
        model_type = (
            AuthenticatedSessionModel
            if principal.actor_type == "user"
            else AnonymousSessionModel
        )
        async with self._database.session() as session:
            stored_hash = await session.scalar(
                select(model_type.csrf_hash).where(model_type.id == principal.session_id)
            )
        if stored_hash is None or not hmac.compare_digest(stored_hash, self._hash(csrf_token)):
            raise AppError(ErrorCode.CSRF_INVALID, "A valid CSRF token is required.")
        return principal

    async def revoke(self, token: str | None, *, now: datetime) -> None:
        principal = await self.authenticate(token, now=now)
        async with self._database.session() as session:
            if principal.actor_type == "user":
                authenticated = await session.get(
                    AuthenticatedSessionModel, principal.session_id
                )
                if authenticated is not None:
                    authenticated.revoked_at = now
            else:
                anonymous = await session.get(AnonymousSessionModel, principal.session_id)
                if anonymous is not None:
                    anonymous.revoked_at = now

    async def _lookup_anonymous(self, token: str, *, now: datetime) -> Principal | None:
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

    async def _lookup_authenticated(
        self, token: str, *, now: datetime
    ) -> SessionGrant | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(AuthenticatedSessionModel, TenantModel, UserModel)
                    .join(TenantModel, TenantModel.id == AuthenticatedSessionModel.tenant_id)
                    .join(UserModel, UserModel.id == AuthenticatedSessionModel.actor_id)
                    .where(
                        AuthenticatedSessionModel.token_hash == self._hash(token),
                        AuthenticatedSessionModel.revoked_at.is_(None),
                        AuthenticatedSessionModel.expires_at > now,
                        TenantModel.status == "active",
                        UserModel.status == "active",
                        UserModel.system_role.is_not(None),
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        model, tenant, user = row
        if user.system_role is None:
            return None
        principal = Principal(
            model.id,
            model.tenant_id,
            model.actor_id,
            actor_type="user",
            role=user.system_role,
        )
        return SessionGrant(
            principal,
            token,
            "",
            model.expires_at,
            False,
            tenant_slug=tenant.slug,
            permissions=SYSTEM_PERMISSIONS,
            email=user.email,
        )

    async def _rotate_authenticated(
        self, grant: SessionGrant, *, token: str, now: datetime
    ) -> SessionGrant:
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + self._duration
        async with self._database.session() as session:
            model = await session.get(AuthenticatedSessionModel, grant.principal.session_id)
            if model is None:
                raise self._authentication_error()
            model.csrf_hash = self._hash(csrf_token)
            model.expires_at = expires_at
        return SessionGrant(
            grant.principal,
            token,
            csrf_token,
            expires_at,
            False,
            tenant_slug=grant.tenant_slug,
            permissions=grant.permissions,
            email=grant.email,
        )

    async def _create_authenticated(
        self, user: UserModel, tenant: TenantModel, *, now: datetime
    ) -> SessionGrant:
        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + self._duration
        session_id = new_uuid7()
        async with self._database.session() as session:
            session.add(
                AuthenticatedSessionModel(
                    id=session_id,
                    token_hash=self._hash(raw_token),
                    csrf_hash=self._hash(csrf_token),
                    tenant_id=tenant.id,
                    actor_id=user.id,
                    expires_at=expires_at,
                )
            )
        role = user.system_role or "system_admin"
        principal = Principal(session_id, tenant.id, user.id, actor_type="user", role=role)
        return SessionGrant(
            principal,
            raw_token,
            csrf_token,
            expires_at,
            True,
            tenant_slug=tenant.slug,
            permissions=SYSTEM_PERMISSIONS,
            email=user.email,
        )

    async def _bootstrap_admin(self) -> None:
        if self._admin_email is None or self._admin_password is None:
            return
        async with self._database.session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "system-admin-bootstrap"},
            )
            existing_admin = await session.scalar(
                select(UserModel).where(UserModel.system_role.is_not(None)).limit(1)
            )
            if existing_admin is not None:
                return
            tenant = await session.scalar(
                select(TenantModel).where(TenantModel.slug == "system-admin")
            )
            if tenant is None:
                tenant = TenantModel(
                    id=ADMIN_TENANT_ID,
                    name="System Administration",
                    slug="system-admin",
                )
                session.add(tenant)
                await session.flush()
            user = await session.scalar(
                select(UserModel).where(func.lower(UserModel.email) == self._admin_email)
            )
            if user is None:
                user = UserModel(
                    id=new_uuid7(),
                    email=self._admin_email,
                    password_hash=self._passwords.hash(self._admin_password),
                    system_role="super_admin",
                )
                session.add(user)
                await session.flush()
            else:
                user.password_hash = self._passwords.hash(self._admin_password)
                user.system_role = "super_admin"
            membership = await session.get(MembershipModel, (tenant.id, user.id))
            if membership is None:
                session.add(
                    MembershipModel(tenant_id=tenant.id, user_id=user.id, role="tenant_admin")
                )

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

    def _verify_password(self, encoded: str, password: str) -> bool:
        try:
            return self._passwords.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False

    def _hash(self, value: str) -> str:
        return hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise self._authentication_error()

    @staticmethod
    def _authentication_error() -> AppError:
        return AppError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            "The email or password is invalid, or the session has expired.",
        )
