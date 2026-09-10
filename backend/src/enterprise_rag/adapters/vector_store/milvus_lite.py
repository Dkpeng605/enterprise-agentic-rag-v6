"""Milvus Lite implementation of the VectorStore contract."""

import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pymilvus import DataType, MilvusClient  # type: ignore[import-untyped]

from enterprise_rag.domain.common import require_uuid7, to_json_value
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.vector_store import (
    DenseSearchRequest,
    IndexSchema,
    SparseSearchRequest,
    UpsertResult,
    VectorHit,
    VectorRecord,
)

OUTPUT_FIELDS = ["root_id", "metadata"]


def _int_value(value: object) -> int:
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError("Milvus response count is not numeric")


class MilvusLiteVectorStore:
    """A single-process local projection store serialized behind an async lock."""

    def __init__(self, database_path: str | Path, *, collection_prefix: str = "rag") -> None:
        path = Path(database_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client = MilvusClient(str(path))
        self._prefix = collection_prefix
        self._dimensions: dict[str, int] = {}
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
                milvus_schema.add_field(
                    field_name="sparse_vector",
                    datatype=DataType.SPARSE_FLOAT_VECTOR,
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
                    metric_type="IP",
                )
                await asyncio.to_thread(
                    self._client.create_collection,
                    collection_name=collection_name,
                    schema=milvus_schema,
                    index_params=index_params,
                    consistency_level="Strong",
                )
            self._dimensions[schema.revision] = schema.dimension

    async def upsert(self, records: Sequence[VectorRecord]) -> UpsertResult:
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
            grouped[record.index_revision].append(record)

        count = 0
        async with self._lock:
            for revision, revision_records in grouped.items():
                data = [self._record_data(record) for record in revision_records]
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
        if request.index_revision not in self._dimensions:
            raise ValueError("index revision must be ensured before search")
        return await self._search(
            revision=request.index_revision,
            data=[dict(request.vector)],
            vector_field="sparse_vector",
            metric_type="IP",
            filter_expression=self._search_filter(
                request.tenant_id,
                collection_ids=request.collection_ids,
                document_ids=request.document_ids,
            ),
            top_k=request.top_k,
        )

    async def delete_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        self._ensure_open()
        require_uuid7(tenant_id, "tenant_id")
        require_uuid7(version_id, "version_id")
        expression = (
            f'tenant_id == "{tenant_id}" and version_id == "{version_id}"'
        )
        count = 0
        async with self._lock:
            for collection_name in await self._collection_names():
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
        self._ensure_open()
        require_uuid7(tenant_id, "tenant_id")
        require_uuid7(version_id, "version_id")
        expression = (
            f'tenant_id == "{tenant_id}" and version_id == "{version_id}"'
        )
        count = 0
        async with self._lock:
            for collection_name in await self._collection_names():
                result = await asyncio.to_thread(
                    self._client.query,
                    collection_name=collection_name,
                    filter=expression,
                    output_fields=["count(*)"],
                )
                count += self._count_rows(result)
        return count

    async def aclose(self) -> None:
        if self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            await asyncio.to_thread(self._client.close)
            self._closed = True
            self._dimensions.clear()

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
    def _record_data(record: VectorRecord) -> dict[str, object]:
        return {
            "leaf_id": record.leaf_id,
            "root_id": record.root_id,
            "tenant_id": str(record.tenant_id),
            "collection_id": str(record.collection_id),
            "document_id": str(record.document_id),
            "version_id": str(record.version_id),
            "status": record.status,
            "dense_vector": list(record.dense_vector),
            "sparse_vector": dict(record.sparse_vector),
            "metadata": to_json_value(record.metadata),
        }

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
