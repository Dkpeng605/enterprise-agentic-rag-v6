"""Dense embedding Provider implementations."""

from enterprise_rag.adapters.embeddings.fastembed_local import (
    BGE_SMALL_ZH_MODEL,
    DEFAULT_MODEL,
    FASTEMBED_MODEL_PROFILES,
    FastEmbedModelProfile,
    LocalMultilingualEmbedding,
)
from enterprise_rag.adapters.embeddings.hashing import HashingDenseEmbedding
from enterprise_rag.adapters.embeddings.openai_compatible import OpenAICompatibleEmbedding

__all__ = [
    "HashingDenseEmbedding",
    "BGE_SMALL_ZH_MODEL",
    "DEFAULT_MODEL",
    "FASTEMBED_MODEL_PROFILES",
    "FastEmbedModelProfile",
    "LocalMultilingualEmbedding",
    "OpenAICompatibleEmbedding",
]
