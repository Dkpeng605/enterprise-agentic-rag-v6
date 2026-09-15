"""Milvus Lite implementation of the VectorStore contract."""

import asyncio
import os
from collections import defaultdict
from collections.abc import Awaitable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from enterprise_rag.domain.common import require_uuid7, to_json_value
from enterprise_rag.observability import current_metrics
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.sparse import SparseMode
from enterprise_rag.ports.vector_store import (
    DenseSearchRequest,
    IndexSchema,
    SparseSearchRequest,
    UpsertResult,
    VectorHit,
    VectorProjection,
    VectorRecord,
)

OUTPUT_FIELDS = ["root_id", "metadata"]


def _import_pymilvus() -> tuple[Any, Any, Any, Any]:
    """Import pymilvus without allowing its settings module to load an app .env file."""

    disabled_before = os.environ.get("PYTHON_DOTENV_DISABLED")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    try:
        from pymilvus import DataType as data_type  # type: ignore[import-untyped]
        from pymilvus import Function as function
        from pymilvus import FunctionType as function_type
        from pymilvus import MilvusClient as client

        return data_type, function, function_type, client
    finally:
        if disabled_before is None:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)
        else:
            os.environ["PYTHON_DOTENV_DISABLED"] = disabled_before


DataType, Function, FunctionType, MilvusClient = _import_pymilvus()


def _int_value(value: object) -> int:
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError("Milvus response count is not numeric")


async def _observe_milvus[ResultT](
    operation: str, awaitable: Awaitable[ResultT]
) -> ResultT:
    status = "error"
    try:
        result = await awaitable
        status = "success"
        return result
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if (metrics := current_metrics()) is not None:
            metrics.observe_milvus(operation=operation, status=status)


class MilvusLiteVectorStore:
    """A single-process local projection store serialized behind an async lock."""

    def __init__(self, database_path: str | Path, *, collection_prefix: str = "rag") -> None:
        path = Path(database_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client = MilvusClient(str(path))
        self._prefix = collection_prefix
        self._dimensions: dict[str, int] = {}
        self._sparse_modes: dict[str, SparseMode] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.VECTOR_STORE,
            name="milvus_lite",
            version="1",
            capabilities=frozenset(
                {"dense_search", "sparse_search", "metadata_filter", "delete", "count"}
            ),
            is_remote=False,
            health=(ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY),
        )

    async def ensure_revision(self, schema: IndexSchema) -> None:
        await _observe_milvus("ensure_revision", self._ensure_revision(schema))

    async def _ensure_revision(self, schema: IndexSchema) -> None:
        self._ensure_open()
        collection_name = self._collection_name(schema.revision)
        async with self._lock:
            exists = await asyncio.to_thread(self._client.has_collection, collection_name)
            if exists:
                description = await asyncio.to_thread(
                    self._client.describe_collection, collection_name
                )
                actual_dimension = self._dense_dimension(description)
                if actual_dimension != schema.dimension:
                    raise ValueError("existing Milvus revision has a different dimension")
                actual_sparse_mode = self._sparse_mode(description)
                if actual_sparse_mode is not schema.sparse_mode:
                    raise ValueError("existing Milvus revision has a different sparse mode")
            else:
                milvus_schema = MilvusClient.create_schema(
                    auto_id=False,
                    enable_dynamic_field=False,
                )
                milvus_schema.add_field(
                    field_name="leaf_id",
                    datatype=DataType.VARCHAR,
                    is_primary=True,
                    max_length=69,
                )
                for field_name in (
                    "tenant_id",
                    "collection_id",
                    "document_id",
                    "version_id",
                ):
                    milvus_schema.add_field(
                        field_name=field_name,
                        datatype=DataType.VARCHAR,
                        max_length=36,
                    )
                milvus_schema.add_field(
                    field_name="root_id", datatype=DataType.VARCHAR, max_length=69
                )
                milvus_schema.add_field(
                    field_name="status", datatype=DataType.VARCHAR, max_length=20
                )
                milvus_schema.add_field(
                    field_name="dense_vector",
                    datatype=DataType.FLOAT_VECTOR,
                    dim=schema.dimension,
                )
                if schema.sparse_mode is SparseMode.MILVUS_BUILTIN_BM25:
                    milvus_schema.add_field(
                        field_name="retrieval_text",
                        datatype=DataType.VARCHAR,
                        max_length=65_535,
                        enable_analyzer=True,
                        analyzer_params={"tokenizer": "jieba"},
                    )
                milvus_schema.add_field(
                    field_name="sparse_vector",
                    datatype=DataType.SPARSE_FLOAT_VECTOR,
                )
                if schema.sparse_mode is SparseMode.MILVUS_BUILTIN_BM25:
                    milvus_schema.add_function(
                        Function(
                            name="retrieval_text_bm25",
                            function_type=FunctionType.BM25,
                            input_field_names=["retrieval_text"],
                            output_field_names=["sparse_vector"],
                        )
                    )
                milvus_schema.add_field(field_name="metadata", datatype=DataType.JSON)

                index_params = self._client.prepare_index_params()
                index_params.add_index(
                    field_name="dense_vector",
                    index_type="AUTOINDEX",
                    metric_type="COSINE",
                )
                index_params.add_index(
                    field_name="sparse_vector",
                    index_type="SPARSE_INVERTED_INDEX",
                    metric_type=(
                        "BM25"
                        if schema.sparse_mode is SparseMode.MILVUS_BUILTIN_BM25
                        else "IP"
                    ),
                )
                await asyncio.to_thread(
                    self._client.create_collection,
                    collection_name=collection_name,
                    schema=milvus_schema,
                    index_params=index_params,
                    consistency_level="Strong",
                )
            await asyncio.to_thread(self._client.load_collection, collection_name)
            self._dimensions[schema.revision] = schema.dimension
            self._sparse_modes[schema.revision] = schema.sparse_mode

    async def upsert(self, records: Sequence[VectorRecord]) -> UpsertResult:
        return await _observe_milvus("upsert", self._upsert(records))

    async def _upsert(self, records: Sequence[VectorRecord]) -> UpsertResult:
        self._ensure_open()
        if not records:
            return UpsertResult(0)
        grouped: dict[str, list[VectorRecord]] = defaultdict(list)
        for record in records:
            dimension = self._dimensions.get(record.index_revision)
            if dimension is None:
                raise ValueError("index revision must be ensured before upsert")
            if len(record.dense_vector) != dimension:
                raise ValueError("dense vector dimension does not match index schema")
            sparse_mode = self._sparse_modes[record.index_revision]
            if sparse_mode is SparseMode.PRECOMPUTED and record.sparse_vector is None:
                raise ValueError("precomputed sparse revision requires sparse vectors")
            if sparse_mode is SparseMode.MILVUS_BUILTIN_BM25 and record.retrieval_text is None:
                raise ValueError("BM25 sparse revision requires retrieval text")
            grouped[record.index_revision].append(record)

        count = 0
        async with self._lock:
            for revision, revision_records in grouped.items():
                sparse_mode = self._sparse_modes[revision]
                data = [
                    self._record_data(record, sparse_mode) for record in revision_records
                ]
                result = await asyncio.to_thread(
                    self._client.upsert,
                    collection_name=self._collection_name(revision),
                    data=data,
                )
                count += _int_value(
                    cast(Mapping[str, object], result).get("upsert_count", len(data))
                )
                await asyncio.to_thread(
                    self._client.flush,
                    collection_name=self._collection_name(revision),
                )
        return UpsertResult(count)

    async def dense_search(self, request: DenseSearchRequest) -> list[VectorHit]:
        return await _observe_milvus("dense_search", self._dense_search(request))

    async def _dense_search(self, request: DenseSearchRequest) -> list[VectorHit]:
        dimension = self._dimensions.get(request.index_revision)
        if dimension is None:
            raise ValueError("index revision must be ensured before search")
        if len(request.vector) != dimension:
            raise ValueError("dense query dimension does not match index schema")
        return await self._search(
            revision=request.index_revision,
            data=[list(request.vector)],
            vector_field="dense_vector",
            metric_type="COSINE",
            filter_expression=self._search_filter(
                request.tenant_id,
                collection_ids=request.collection_ids,
                document_ids=request.document_ids,
            ),
            top_k=request.top_k,
        )

    async def sparse_search(self, request: SparseSearchRequest) -> list[VectorHit]:
        return await _observe_milvus("sparse_search", self._sparse_search(request))

    async def _sparse_search(self, request: SparseSearchRequest) -> list[VectorHit]:
        if request.index_revision not in self._dimensions:
            raise ValueError("index revision must be ensured before search")
        sparse_mode = self._sparse_modes[request.index_revision]
        if sparse_mode is SparseMode.PRECOMPUTED:
            if request.vector is None:
                raise ValueError("precomputed sparse revision requires a query vector")
            data: list[object] = [dict(request.vector)]
            metric_type = "IP"
        else:
            if request.query_text is None:
                raise ValueError("BM25 sparse revision requires query text")
            data = [request.query_text]
            metric_type = "BM25"
        return await self._search(
            revision=request.index_revision,
            data=data,
            vector_field="sparse_vector",
            metric_type=metric_type,
            filter_expression=self._search_filter(
                request.tenant_id,
                collection_ids=request.collection_ids,
                document_ids=request.document_ids,
            ),
            top_k=request.top_k,
        )

    async def delete_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        return await _observe_milvus(
            "delete_by_version", self._delete_by_version(tenant_id, version_id)
        )

    async def _delete_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        self._ensure_open()
        require_uuid7(tenant_id, "tenant_id")
        require_uuid7(version_id, "version_id")
        expression = (
            f'tenant_id == "{tenant_id}" and version_id == "{version_id}"'
        )
        count = 0
        async with self._lock:
            for collection_name in await self._collection_names():
                await self._load_collection(collection_name)
                rows = await asyncio.to_thread(
                    self._client.query,
                    collection_name=collection_name,
                    filter=expression,
                    output_fields=["count(*)"],
                )
                count += self._count_rows(rows)
                await asyncio.to_thread(
                    self._client.delete,
                    collection_name=collection_name,
                    filter=expression,
                )
                await asyncio.to_thread(
                    self._client.flush,
                    collection_name=collection_name,
                )
        return count

    async def count_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        return await _observe_milvus(
            "count_by_version", self._count_by_version(tenant_id, version_id)
        )

    async def delete_by_version_revision(
        self, tenant_id: UUID, version_id: UUID, index_revision: str
    ) -> int:
        return await _observe_milvus(
            "delete_by_version_revision",
            self._delete_by_version_revision(tenant_id, version_id, index_revision),
        )

    async def _delete_by_version_revision(
        self, tenant_id: UUID, version_id: UUID, index_revision: str
    ) -> int:
        self._ensure_open()
        require_uuid7(tenant_id, "tenant_id")
        require_uuid7(version_id, "version_id")
        if not index_revision.strip():
            raise ValueError("index_revision must not be empty")
        expression = f'tenant_id == "{tenant_id}" and version_id == "{version_id}"'
        collection_name = self._collection_name(index_revision)
        count = 0
        async with self._lock:
            if not await asyncio.to_thread(self._client.has_collection, collection_name):
                return 0
            await self._load_collection(collection_name)
            rows = await asyncio.to_thread(
                self._client.query,
                collection_name=collection_name,
                filter=expression,
                output_fields=["count(*)"],
            )
            count = self._count_rows(rows)
            await asyncio.to_thread(
                self._client.delete, collection_name=collection_name, filter=expression
            )
            await asyncio.to_thread(self._client.flush, collection_name=collection_name)
        return count

    async def count_by_version_revision(
        self, tenant_id: UUID, version_id: UUID, index_revision: str
    ) -> int:
        return await _observe_milvus(
            "count_by_version_revision",
            self._count_by_version_revision(tenant_id, version_id, index_revision),
        )

    async def _count_by_version_revision(
        self, tenant_id: UUID, version_id: UUID, index_revision: str
    ) -> int:
        self._ensure_open()
        require_uuid7(tenant_id, "tenant_id")
        require_uuid7(version_id, "version_id")
        if not index_revision.strip():
            raise ValueError("index_revision must not be empty")
        collection_name = self._collection_name(index_revision)
        expression = f'tenant_id == "{tenant_id}" and version_id == "{version_id}"'
        async with self._lock:
            if not await asyncio.to_thread(self._client.has_collection, collection_name):
                return 0
            await self._load_collection(collection_name)
            result = await asyncio.to_thread(
                self._client.query,
                collection_name=collection_name,
                filter=expression,
                output_fields=["count(*)"],
            )
        return self._count_rows(result)

    async def _count_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        self._ensure_open()
        require_uuid7(tenant_id, "tenant_id")
        require_uuid7(version_id, "version_id")
        expression = (
            f'tenant_id == "{tenant_id}" and version_id == "{version_id}"'
        )
        count = 0
        async with self._lock:
            for collection_name in await self._collection_names():
                await self._load_collection(collection_name)
                result = await asyncio.to_thread(
                    self._client.query,
                    collection_name=collection_name,
                    filter=expression,
                    output_fields=["count(*)"],
                )
                count += self._count_rows(result)
        return count

    async def list_version_projections(self) -> tuple[VectorProjection, ...]:
        return await _observe_milvus(
            "list_version_projections", self._list_version_projections()
        )

    async def _list_version_projections(self) -> tuple[VectorProjection, ...]:
        """Return aggregate projection ownership without exposing vector payloads."""

        self._ensure_open()
        counts: dict[tuple[UUID, UUID], int] = defaultdict(int)
        async with self._lock:
            for collection_name in await self._collection_names():
                await self._load_collection(collection_name)
                rows = await asyncio.to_thread(
                    self._projection_rows,
                    collection_name,
                )
                for row in rows:
                    key = (UUID(str(row["tenant_id"])), UUID(str(row["version_id"])))
                    counts[key] += 1
        return tuple(
            VectorProjection(tenant_id=key[0], version_id=key[1], count=count)
            for key, count in sorted(
                counts.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
            )
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            await asyncio.to_thread(self._client.close)
            self._closed = True
            self._dimensions.clear()
            self._sparse_modes.clear()

    async def _search(
        self,
        *,
        revision: str,
        data: list[object],
        vector_field: str,
        metric_type: str,
        filter_expression: str,
        top_k: int,
    ) -> list[VectorHit]:
        self._ensure_open()
        async with self._lock:
            raw = await asyncio.to_thread(
                self._client.search,
                collection_name=self._collection_name(revision),
                data=data,
                anns_field=vector_field,
                filter=filter_expression,
                limit=top_k,
                output_fields=OUTPUT_FIELDS,
                search_params={"metric_type": metric_type, "params": {}},
            )
        result_sets = cast(list[list[Mapping[str, Any]]], raw)
        if not result_sets:
            return []
        hits: list[VectorHit] = []
        for item in result_sets[0]:
            entity = cast(Mapping[str, object], item.get("entity", {}))
            leaf_id = item.get("leaf_id", entity.get("leaf_id"))
            if leaf_id is None:
                raise ValueError("Milvus search result is missing its primary key")
            hits.append(
                VectorHit(
                    leaf_id=str(leaf_id),
                    root_id=str(entity["root_id"]),
                    score=float(item["distance"]),
                    metadata=cast(Mapping[str, object], entity.get("metadata", {})),
                )
            )
        return hits

    async def _collection_names(self) -> tuple[str, ...]:
        names = await asyncio.to_thread(self._client.list_collections)
        return tuple(name for name in names if name.startswith(f"{self._prefix}_"))

    async def _load_collection(self, collection_name: str) -> None:
        """Re-open persisted Milvus Lite collections after a process restart."""

        await asyncio.to_thread(self._client.load_collection, collection_name)

    def _projection_rows(self, collection_name: str) -> list[Mapping[str, object]]:
        iterator = self._client.query_iterator(
            collection_name=collection_name,
            batch_size=1000,
            limit=-1,
            filter="",
            output_fields=["tenant_id", "version_id"],
        )
        rows: list[Mapping[str, object]] = []
        try:
            while batch := iterator.next():
                rows.extend(cast(list[Mapping[str, object]], batch))
        finally:
            iterator.close()
        return rows

    def _collection_name(self, revision: str) -> str:
        suffix = sha256(revision.encode("utf-8")).hexdigest()[:24]
        return f"{self._prefix}_{suffix}"

    @staticmethod
    def _count_rows(result: object) -> int:
        rows = cast(list[Mapping[str, object]], result)
        return 0 if not rows else _int_value(rows[0].get("count(*)", 0))

    @staticmethod
    def _dense_dimension(description: Mapping[str, Any]) -> int:
        fields = cast(Sequence[Mapping[str, Any]], description.get("fields", ()))
        for field in fields:
            if field.get("name") == "dense_vector":
                params = cast(Mapping[str, object], field.get("params", {}))
                return _int_value(params["dim"])
        raise ValueError("existing Milvus revision has no dense vector field")

    @staticmethod
    def _sparse_mode(description: Mapping[str, Any]) -> SparseMode:
        fields = cast(Sequence[Mapping[str, Any]], description.get("fields", ()))
        names = {str(field.get("name")) for field in fields}
        if "retrieval_text" in names:
            return SparseMode.MILVUS_BUILTIN_BM25
        if "sparse_vector" in names:
            return SparseMode.PRECOMPUTED
        raise ValueError("existing Milvus revision has no sparse vector field")

    @staticmethod
    def _record_data(record: VectorRecord, sparse_mode: SparseMode) -> dict[str, object]:
        data: dict[str, object] = {
            "leaf_id": record.leaf_id,
            "root_id": record.root_id,
            "tenant_id": str(record.tenant_id),
            "collection_id": str(record.collection_id),
            "document_id": str(record.document_id),
            "version_id": str(record.version_id),
            "status": record.status,
            "dense_vector": list(record.dense_vector),
            "metadata": to_json_value(record.metadata),
        }
        if sparse_mode is SparseMode.PRECOMPUTED:
            if record.sparse_vector is None:
                raise ValueError("precomputed sparse revision requires sparse vectors")
            data["sparse_vector"] = dict(record.sparse_vector)
        else:
            if record.retrieval_text is None:
                raise ValueError("BM25 sparse revision requires retrieval text")
            data["retrieval_text"] = record.retrieval_text
        return data

    @staticmethod
    def _search_filter(
        tenant_id: UUID,
        *,
        collection_ids: Sequence[UUID],
        document_ids: Sequence[UUID],
    ) -> str:
        filters = [f'tenant_id == "{tenant_id}"', 'status == "ready"']
        if collection_ids:
            values = ", ".join(f'"{value}"' for value in collection_ids)
            filters.append(f"collection_id in [{values}]")
        if document_ids:
            values = ", ".join(f'"{value}"' for value in document_ids)
            filters.append(f"document_id in [{values}]")
        return " and ".join(filters)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Milvus Lite vector store is closed")
