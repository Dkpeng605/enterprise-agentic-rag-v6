"""Dense embedding Provider implementations."""

from enterprise_rag.adapters.embeddings.fastembed_local import LocalMultilingualEmbedding
from enterprise_rag.adapters.embeddings.hashing import HashingDenseEmbedding
from enterprise_rag.adapters.embeddings.openai_compatible import OpenAICompatibleEmbedding

__all__ = [
    "HashingDenseEmbedding",
    "LocalMultilingualEmbedding",
    "OpenAICompatibleEmbedding",
]
