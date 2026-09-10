"""Dense embedding Provider implementations."""

from enterprise_rag.adapters.embeddings.fastembed_local import LocalMultilingualEmbedding
from enterprise_rag.adapters.embeddings.openai_compatible import OpenAICompatibleEmbedding

__all__ = ["LocalMultilingualEmbedding", "OpenAICompatibleEmbedding"]
