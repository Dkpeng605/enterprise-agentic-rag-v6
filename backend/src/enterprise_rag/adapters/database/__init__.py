"""PostgreSQL persistence adapter."""

from enterprise_rag.adapters.database.context import (
    PostgreSQLContextRepository,
    ScopeConflictError,
)
from enterprise_rag.adapters.database.documents import (
    DocumentRegistration,
    DocumentRegistrationError,
    DocumentRegistrationRepository,
)
from enterprise_rag.adapters.database.engine import Database
from enterprise_rag.adapters.database.ingestion import (
    IngestionContentRepository,
    IngestionPersistenceError,
    IngestionWork,
)
from enterprise_rag.adapters.database.jobs import IngestionJobRepository, JobStateError
from enterprise_rag.adapters.database.lifecycle import (
    DeleteRequest,
    DeletionContext,
    DocumentLifecycleError,
    DocumentLifecycleRepository,
)
from enterprise_rag.adapters.database.mcp_tokens import PostgreSQLMcpTokenStore
from enterprise_rag.adapters.database.models import Base
from enterprise_rag.adapters.database.repository import AsyncRepository
from enterprise_rag.adapters.database.traces import PostgreSQLTraceStore
from enterprise_rag.adapters.database.usage import PostgreSQLUsageStore

__all__ = [
    "AsyncRepository",
    "Base",
    "PostgreSQLContextRepository",
    "PostgreSQLUsageStore",
    "PostgreSQLTraceStore",
    "PostgreSQLMcpTokenStore",
    "ScopeConflictError",
    "Database",
    "DocumentRegistration",
    "DocumentRegistrationError",
    "DocumentRegistrationRepository",
    "DeleteRequest",
    "DeletionContext",
    "DocumentLifecycleError",
    "DocumentLifecycleRepository",
    "IngestionJobRepository",
    "IngestionContentRepository",
    "IngestionPersistenceError",
    "IngestionWork",
    "JobStateError",
]
