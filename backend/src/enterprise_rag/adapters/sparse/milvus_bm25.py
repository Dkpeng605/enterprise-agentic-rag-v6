"""Text-backed sparse provider for Milvus native BM25 Function indexes."""

from collections.abc import Sequence

from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.sparse import SparseEncoding, SparseMode


class MilvusBuiltinBm25Encoder:
    """Pass lexical text to Milvus; Milvus owns tokenization, TF, IDF, and scoring."""

    def __init__(self) -> None:
        self._closed = False

    @property
    def mode(self) -> SparseMode:
        return SparseMode.MILVUS_BUILTIN_BM25

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.SPARSE_ENCODER,
            name="milvus_builtin_bm25",
            version="jieba-v1",
            capabilities=frozenset(
                {"documents", "query", "bm25", "corpus_idf", "jieba", "multilingual"}
            ),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def encode_documents(self, texts: Sequence[str]) -> list[SparseEncoding]:
        self._ensure_open()
        return [SparseEncoding(self.mode, text=text) for text in texts]

    async def encode_query(self, text: str) -> SparseEncoding:
        self._ensure_open()
        return SparseEncoding(self.mode, text=text)

    async def aclose(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Sparse Encoder is closed")
