# ---------------------------------------------------------------------------
# Purpose: LLM-based re-ranking for semantic search and cross-job matching
# ---------------------------------------------------------------------------
#
# Two-stage retrieve-then-rerank pattern (Phase 5):
#
#   Stage 1 — pgvector pre-filter narrows the candidate set quickly using
#             embeddings (free, sub-millisecond per candidate).
#   Stage 2 — this module's `rerank_*` functions send the actual TEXT of each
#             surviving candidate to Gemini for accurate scoring.
#
# The split exists because dense embedding models inflate similarity for
# semantically-adjacent-but-technically-different content (e.g. "Python dev"
# vs "Java dev" can score 0.7+). Reading the actual words and judging on
# specific technical content is something the LLM does correctly but
# embeddings cannot.
#
# Results are cached in Redis with a 1-hour TTL because LLM calls are
# expensive. The bulk match recheck flow in particular benefits — most calls
# turn into cache hits when resumes and jobs haven't changed.

import asyncio
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI
from redis import Redis
from redis.exceptions import RedisError

from app.config import settings


@dataclass(frozen=True)
class RerankResult:
    """One LLM rerank score for a (query, candidate) pair."""
    score: int       # 0–100
    critique: str    # human-readable reasoning from the LLM


class RerankError(Exception):
    """
    Raised when LLM rerank fails (network, parsing, quota). Callers should
    fall back to vector-only scoring rather than break the user experience.
    """


# Caching --------------------------------------------------------------------

RERANK_CACHE_TTL_SECONDS = 3600  # 1 hour — same as the Phase 2 embedding cache

_redis_client: Redis | None = None


def _get_redis_client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.RATE_LIMITER_STORAGE_URL,
            decode_responses=True,
        )
    return _redis_client


def _cache_key(application_id: int, query_text: str, candidate_text: str) -> str:
    """
    Cache key includes application_id so /reanalyze can invalidate by pattern.
    A hash of (query + candidate) makes the key unique per pair without
    bloating Redis with long string keys.
    """
    payload = f"{query_text}\n---\n{candidate_text}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"rerank:{application_id}:{digest}"


def _check_cache(application_id: int, query_text: str, candidate_text: str) -> RerankResult | None:
    try:
        cached = _get_redis_client().get(_cache_key(application_id, query_text, candidate_text))
        if cached:
            data = json.loads(cached)
            return RerankResult(score=int(data["score"]), critique=str(data["critique"]))
    except (RedisError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        pass
    return None


def _save_to_cache(application_id: int, query_text: str, candidate_text: str, result: RerankResult) -> None:
    try:
        _get_redis_client().setex(
            _cache_key(application_id, query_text, candidate_text),
            RERANK_CACHE_TTL_SECONDS,
            json.dumps({"score": result.score, "critique": result.critique}),
        )
    except RedisError:
        # Failure to cache is not a real error — the score is already computed
        pass


def clear_application_rerank_cache(application_id: int) -> int:
    """
    Delete every rerank cache entry for an application. Called by /reanalyze
    when the underlying resume has been re-extracted; prior cached scores
    referenced the old resume content and are now stale.

    Returns the number of entries deleted. Uses SCAN for safety (not KEYS).
    """
    try:
        client = _get_redis_client()
        pattern = f"rerank:{application_id}:*"
        keys = list(client.scan_iter(match=pattern))
        if not keys:
            return 0
        return client.delete(*keys)
    except RedisError:
        return 0


# LLM scoring ----------------------------------------------------------------

# Low temperature for consistent / less generous scoring. The prompt also
# tells the LLM to be strict, which we observed during testing matters more
# than temperature alone for getting honest scores.
RERANK_TEMPERATURE = 0.1

RERANK_PROMPT_TEMPLATE = """You are an expert recruiter. Score how well this resume fits the role or query described below. Be honest and strict — consider specific technical skills, years of experience, role level, and domain knowledge.

CRITICAL RULES:
1. A resume that does not mention a required technology should score significantly lower, even if it mentions similar technologies.
2. A resume for the wrong language / framework family (e.g. Python developer for a Java role) should score under 40, not 70+, regardless of how strong the candidate is in their own stack.
3. Years of experience and role level matter — a junior candidate for a senior role should score lower.
4. Don't reward generic "software engineering" overlap — score on the specific match.

JOB / QUERY:
{query_text}

RESUME:
{resume_text}

Return a strict JSON object with NO surrounding text, NO markdown fences, NO commentary:
{{
    "score": <integer 0-100>,
    "critique": <one-paragraph explanation, max 300 chars, of what matched and what did not>
}}"""


@lru_cache(maxsize=1)
def _get_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=settings.GEMINI_API_KEY,
        temperature=RERANK_TEMPERATURE,
    )


def _parse_llm_response(text: str) -> RerankResult:
    """
    Parse Gemini's JSON output. Tolerates markdown code fences that the model
    sometimes wraps the response in despite being told not to.
    """
    cleaned = text.strip()
    # Strip ```json … ``` or ``` … ``` fences if present
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if "```" in cleaned:
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RerankError(f"LLM returned non-JSON: {cleaned!r}") from e

    raw_score = data.get("score", 0)
    try:
        score = int(raw_score)
    except (ValueError, TypeError):
        score = 0
    score = max(0, min(100, score))

    critique = str(data.get("critique", "")).strip()
    return RerankResult(score=score, critique=critique)


def _score_one(application_id: int, query_text: str, candidate_text: str) -> RerankResult:
    """
    Score one (query, candidate) pair. Checks the cache first, falls through
    to an LLM call on miss. Raises RerankError on any failure so the caller
    can decide whether to fall back to vector-only.
    """
    cached = _check_cache(application_id, query_text, candidate_text)
    if cached is not None:
        return cached

    prompt = RERANK_PROMPT_TEMPLATE.format(
        query_text=query_text,
        resume_text=candidate_text,
    )

    try:
        response = _get_llm().invoke(prompt)
    except Exception as e:
        raise RerankError(f"Gemini call failed: {e}") from e

    result = _parse_llm_response(response.content)
    _save_to_cache(application_id, query_text, candidate_text, result)
    return result


# Public API -----------------------------------------------------------------


def rerank_sequential(
    pairs: list[tuple[int, str, str]],
) -> list[RerankResult | None]:
    """
    Score each (application_id, query_text, candidate_text) tuple sequentially.

    Used by Celery tasks (Phase 3 cross-job matching) where total latency is
    not user-facing and we want to stay well under Gemini's RPM cap during
    bulk operations. Returns `None` for any individual call that failed so
    the caller can decide per-result whether to fall back to vector scoring.
    """
    results: list[RerankResult | None] = []
    for app_id, query, candidate in pairs:
        try:
            results.append(_score_one(app_id, query, candidate))
        except RerankError as e:
            print(f"Rerank: skipping one pair (app={app_id}): {e}")
            results.append(None)
    return results


async def rerank_parallel(
    pairs: list[tuple[int, str, str]],
) -> list[RerankResult | None]:
    """
    Score each pair in parallel via asyncio.gather + asyncio.to_thread.

    Used by synchronous endpoints (Phase 2 search) so total latency is
    bounded by the slowest single call rather than the sum. With K=10 and
    individual LLM latency of ~1–2 s, the whole rerank step completes in
    1–2 s instead of 10–20 s sequentially.

    Failures are returned as `None` in the result list (gather with
    return_exceptions converts them to entries the caller can filter).
    """

    async def _one(pair: tuple[int, str, str]) -> RerankResult | None:
        try:
            return await asyncio.to_thread(_score_one, *pair)
        except RerankError as e:
            print(f"Rerank: skipping one pair (app={pair[0]}): {e}")
            return None

    return await asyncio.gather(*(_one(p) for p in pairs))
