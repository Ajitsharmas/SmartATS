"""
Unit tests for app/ai.py — Gemini error classifier.

`_classify_gemini_error` decides whether a raw exception from the Gemini SDK
is a daily-quota-exhausted case (GeminiQuotaExhaustedError), a transient
unavailable case (GeminiUnavailableError), or unrecognised (returns None).

These tests exercise the heuristic against synthetic exception shapes that
mimic both the old google-api-core path and the new google-genai SDK path.
"""

from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

from app.ai import (
    GeminiQuotaExhaustedError,
    GeminiUnavailableError,
    _classify_gemini_error,
)


class FakeGenaiError(Exception):
    """Stand-in for a google.genai.errors.* exception with a `.code` attr."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class TestClassifyGeminiError:
    # ----- google-api-core path -----

    def test_service_unavailable_classified_as_transient(self):
        exc = ServiceUnavailable("Backend is overloaded.")
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiUnavailableError)
        # NOT a daily-quota subclass
        assert not isinstance(result, GeminiQuotaExhaustedError)

    def test_resource_exhausted_with_daily_marker(self):
        exc = ResourceExhausted(
            "Quota exceeded for generate_content_free_tier_requests, limit: 20"
        )
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiQuotaExhaustedError)
        # Subclass of GeminiUnavailableError so existing handlers still catch
        assert isinstance(result, GeminiUnavailableError)

    def test_resource_exhausted_without_daily_marker_is_transient(self):
        # Per-minute quota (no "free_tier_requests" / "per_day" marker)
        exc = ResourceExhausted("Rate limited. Try again later.")
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiUnavailableError)
        assert not isinstance(result, GeminiQuotaExhaustedError)

    # ----- new google-genai SDK path -----

    def test_new_sdk_429_with_daily_quota(self):
        msg = (
            "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests"
        )
        exc = FakeGenaiError(msg, code=429)
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiQuotaExhaustedError)

    def test_new_sdk_429_per_day_per_project_marker(self):
        msg = "429 RESOURCE_EXHAUSTED. GenerateRequestsPerDayPerProjectPerModel-FreeTier exceeded"
        exc = FakeGenaiError(msg, code=429)
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiQuotaExhaustedError)

    def test_new_sdk_429_transient(self):
        # 429 but no daily-quota marker → per-minute throttling
        msg = "429 RESOURCE_EXHAUSTED. Rate limit hit, retry in 30s"
        exc = FakeGenaiError(msg, code=429)
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiUnavailableError)
        assert not isinstance(result, GeminiQuotaExhaustedError)

    def test_new_sdk_503(self):
        msg = "503 UNAVAILABLE. The service is currently unavailable."
        exc = FakeGenaiError(msg, code=503)
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiUnavailableError)
        assert not isinstance(result, GeminiQuotaExhaustedError)

    def test_unrelated_exception_returns_none(self):
        # ValueError isn't a Gemini error of any kind
        result = _classify_gemini_error(ValueError("bad input"))
        assert result is None

    def test_runtime_error_returns_none(self):
        result = _classify_gemini_error(RuntimeError("unexpected"))
        assert result is None

    def test_429_message_without_code_attr(self):
        # Exception with the markers in its message but no .code attribute
        # — should still be detected via string scan
        exc = Exception("429 RESOURCE_EXHAUSTED. free_tier_requests exhausted")
        result = _classify_gemini_error(exc)
        # Detected as 429 via message scan; daily-quota markers present
        assert isinstance(result, GeminiQuotaExhaustedError)

    def test_unavailable_message_without_code_attr(self):
        exc = Exception("503 UNAVAILABLE backend issue")
        result = _classify_gemini_error(exc)
        assert isinstance(result, GeminiUnavailableError)
        assert not isinstance(result, GeminiQuotaExhaustedError)
