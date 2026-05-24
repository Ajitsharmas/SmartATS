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

import hashlib
import json
from functools import lru_cache

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from redis import Redis
from redis.exceptions import RedisError

from app.config import settings


class EmbeddingError(Exception):
    """Raised when the embedding provider returns an error or is unreachable."""


# Search-query embeddings are cached for an hour to absorb repeated queries
# without incurring Gemini API calls. Resume / document chunks are NOT cached
# here — they live in pgvector as persistent storage.
QUERY_EMBEDDING_CACHE_TTL_SECONDS = 3600

_redis_client: Redis | None = None


def _get_redis_client() -> Redis:
    """Lazily construct a Redis client for the query-embedding cache."""
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.RATE_LIMITER_STORAGE_URL,
            decode_responses=True,
        )
    return _redis_client


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


def embed_query_cached(query: str) -> list[float]:
    """
    Embed a search query, using Redis to cache the result for an hour.

    Common queries (e.g. "Python engineer", "AWS architect") hit the cache
    and skip the Gemini API call entirely — typical search workloads have
    high repetition, so a substantial fraction of searches end up free.

    If Redis is unreachable for any reason, the function silently falls
    back to a direct Gemini call — caching is an optimisation, not a hard
    dependency for search to work.
    """
    cache_key = f"emb:{hashlib.sha256(query.encode('utf-8')).hexdigest()}"

    try:
        client = _get_redis_client()
        cached = client.get(cache_key)
        if cached:
            return json.loads(cached)
    except (RedisError, json.JSONDecodeError):
        # Cache miss / Redis down / bad cached value — fall through to direct call
        pass

    vector = embed_text(query)

    try:
        _get_redis_client().setex(
            cache_key,
            QUERY_EMBEDDING_CACHE_TTL_SECONDS,
            json.dumps(vector),
        )
    except RedisError:
        # Failed to write to cache — not a problem, we still have the vector
        pass

    return vector
