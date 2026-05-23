# ---------------------------------------------------------------------------
# Purpose: Embedding service — wraps Gemini's gemini-embedding-001 via LangChain
# ---------------------------------------------------------------------------
#
# Provides a small, focused interface around Google's embedding model:
#   - embed_text(text)    → list[float] of length EMBEDDING_DIMENSIONS
#   - embed_texts(texts)  → list[list[float]] (batched)
#
# Why LangChain's wrapper instead of calling Gemini's SDK directly:
#   - One-line interface that LangChain retrievers (used in Feature 1 RAG)
#     can plug into without adapters.
#   - Provider-agnostic: we can swap GoogleGenerativeAIEmbeddings for
#     OpenAIEmbeddings later by changing one line.
#
# Errors from the Gemini API are wrapped in EmbeddingError so callers can
# distinguish them from generic exceptions (e.g., for retry decisions in
# Celery tasks).

from functools import lru_cache

from langchain_google_genai import GoogleGenerativeAIEmbeddings

from app.config import settings


class EmbeddingError(Exception):
    """Raised when the embedding provider returns an error or is unreachable."""


@lru_cache(maxsize=1)
def _get_client() -> GoogleGenerativeAIEmbeddings:
    """
    Lazily construct the embeddings client once per process.
    lru_cache(maxsize=1) ensures only one instance ever exists.

    gemini-embedding-001 defaults to 3072-dim vectors; we explicitly request
    `output_dimensionality` so the output matches our pgvector column size.
    """
    return GoogleGenerativeAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        google_api_key=settings.GEMINI_API_KEY,
        output_dimensionality=settings.EMBEDDING_DIMENSIONS,
    )


def embed_text(text: str) -> list[float]:
    """
    Embed a single piece of text.

    Returns a list of floats with length `settings.EMBEDDING_DIMENSIONS` (768
    by default; `gemini-embedding-001` supports 768, 1536, or 3072). Raises
    `EmbeddingError` on provider failure.
    """
    try:
        return _get_client().embed_query(text)
    except Exception as e:
        raise EmbeddingError(f"Failed to embed text: {e}") from e


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed multiple texts in a single batched API call.

    Use this when embedding many chunks at once (e.g., all chunks of one
    resume) — far more efficient than calling embed_text in a loop because
    the underlying provider batches the network round-trip.
    """
    try:
        return _get_client().embed_documents(texts)
    except Exception as e:
        raise EmbeddingError(f"Failed to embed texts: {e}") from e
