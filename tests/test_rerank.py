"""
Unit tests for app/rerank.py — the LLM rerank module used by Phases 2, 3, 5.

Focuses on:
- _parse_llm_response: markdown-fence-tolerant JSON parsing (same family
  as outreach._parse_response but with slightly different shape — score
  + critique instead of subject + body)
- _cache_key: stable cache key derivation for the Redis rerank cache
- RerankResult dataclass invariants

End-to-end coverage (real Gemini rerank + Redis cache) lives in
scripts/smoke_test_phase5.py.
"""

import pytest

from app.rerank import (
    RerankError,
    RerankResult,
    _cache_key,
    _parse_llm_response,
)


# ---------------------------------------------------------------------------
# _parse_llm_response — defensive JSON parser
# ---------------------------------------------------------------------------

class TestParseLLMResponse:
    def test_clean_json(self):
        text = '{"score": 75, "critique": "Strong Python background."}'
        result = _parse_llm_response(text)
        assert result.score == 75
        assert "Python" in result.critique

    def test_strips_json_fence(self):
        text = '```json\n{"score": 50, "critique": "Mixed fit."}\n```'
        result = _parse_llm_response(text)
        assert result.score == 50

    def test_strips_plain_fence(self):
        text = '```\n{"score": 30, "critique": "Wrong stack."}\n```'
        result = _parse_llm_response(text)
        assert result.score == 30

    def test_score_clamped_low(self):
        # Negative score → clamped to 0
        text = '{"score": -100, "critique": "..."}'
        result = _parse_llm_response(text)
        assert result.score == 0

    def test_score_clamped_high(self):
        # > 100 → clamped to 100
        text = '{"score": 500, "critique": "..."}'
        result = _parse_llm_response(text)
        assert result.score == 100

    def test_score_zero_on_non_integer(self):
        # Garbage in score field → 0, not exception
        text = '{"score": "abc", "critique": "ok"}'
        result = _parse_llm_response(text)
        assert result.score == 0

    def test_score_zero_when_missing(self):
        text = '{"critique": "ok"}'
        result = _parse_llm_response(text)
        assert result.score == 0

    def test_critique_defaults_to_empty(self):
        text = '{"score": 80}'
        result = _parse_llm_response(text)
        assert result.critique == ""

    def test_raises_on_non_json(self):
        with pytest.raises(RerankError, match="non-JSON"):
            _parse_llm_response("not even close to JSON")

    def test_handles_float_score(self):
        # int() coercion of a float string works
        text = '{"score": 75.5, "critique": "ok"}'
        result = _parse_llm_response(text)
        assert result.score == 75  # int conversion truncates


# ---------------------------------------------------------------------------
# _cache_key — Redis cache key stability
# ---------------------------------------------------------------------------

class TestCacheKey:
    def test_cache_key_includes_application_id(self):
        key = _cache_key(42, "python role", "alice resume text")
        assert key.startswith("rerank:42:")

    def test_same_inputs_produce_same_key(self):
        # Critical for cache hits
        k1 = _cache_key(42, "python role", "alice text")
        k2 = _cache_key(42, "python role", "alice text")
        assert k1 == k2

    def test_different_query_different_key(self):
        k1 = _cache_key(42, "python role", "alice")
        k2 = _cache_key(42, "java role", "alice")
        assert k1 != k2

    def test_different_candidate_different_key(self):
        k1 = _cache_key(42, "python role", "alice resume")
        k2 = _cache_key(42, "python role", "bob resume")
        assert k1 != k2

    def test_different_application_id_different_key(self):
        k1 = _cache_key(42, "q", "c")
        k2 = _cache_key(43, "q", "c")
        assert k1 != k2

    def test_cache_key_length_bounded(self):
        # 32-char hex digest + prefix → key length doesn't depend on input size
        short_key = _cache_key(1, "q", "c")
        long_key = _cache_key(1, "q" * 10000, "c" * 10000)
        assert len(short_key) == len(long_key)


# ---------------------------------------------------------------------------
# RerankResult dataclass — frozen invariant
# ---------------------------------------------------------------------------

class TestRerankResult:
    def test_construction(self):
        r = RerankResult(score=85, critique="Strong fit")
        assert r.score == 85
        assert r.critique == "Strong fit"

    def test_is_frozen(self):
        # @dataclass(frozen=True) means attributes can't be mutated
        r = RerankResult(score=85, critique="x")
        with pytest.raises((AttributeError, Exception)):
            r.score = 99  # type: ignore[misc]

    def test_equality_by_value(self):
        a = RerankResult(score=70, critique="ok")
        b = RerankResult(score=70, critique="ok")
        c = RerankResult(score=70, critique="different")
        assert a == b
        assert a != c
