# -------------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: The Abstract AI Strategy & Concrete Implementations
# -------------------------------------------------------------------------------------------------------------------------------------------------

from typing import Protocol, Any
from google import genai
from google.api_core.exceptions import ServiceUnavailable, ResourceExhausted
import httpx
from app.config import settings


class GeminiUnavailableError(Exception):
    """Raised when Gemini returns a transient 503/429 error worth retrying."""


class GeminiQuotaExhaustedError(GeminiUnavailableError):
    """
    Raised specifically when Gemini's free-tier *daily* quota is exhausted
    (not just per-minute rate-limited). Subclass of GeminiUnavailableError
    so existing `except GeminiUnavailableError` handlers still catch it —
    callers that want to distinguish "wait a few minutes" from "wait until
    tomorrow" can catch this subclass first.
    """


def _classify_gemini_error(exc: BaseException) -> GeminiUnavailableError | None:
    """
    Map a raw Gemini-side exception to one of our domain exceptions.

    Returns:
      - GeminiQuotaExhaustedError if the error indicates the daily free-tier
        quota is used up (recruiter needs to wait until tomorrow or upgrade)
      - GeminiUnavailableError if the error is transient (high demand / 503)
      - None if the error doesn't look like either — caller should re-raise
        the original exception unchanged
    """
    if isinstance(exc, ResourceExhausted):
        msg = str(exc)
    elif isinstance(exc, ServiceUnavailable):
        return GeminiUnavailableError(str(exc))
    else:
        # New google-genai SDK — errors are google.genai.errors.* with a
        # `.code` attribute (HTTP status) and a stringified body.
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        msg = str(exc)
        is_429 = code == 429 or "RESOURCE_EXHAUSTED" in msg or " 429 " in msg or msg.startswith("429")
        is_503 = code == 503 or "UNAVAILABLE" in msg
        if not (is_429 or is_503):
            return None
        if is_503:
            return GeminiUnavailableError(msg)

    # 429 from either SDK — figure out daily-quota vs per-minute rate limit.
    # Daily quota errors mention "_free_tier_requests", "GenerateRequestsPerDay",
    # or "quotaValue" with the daily limit. Per-minute throttling typically
    # just says "Quota exceeded" without those markers, and Google retries it
    # automatically inside the SDK before bubbling up to us.
    lowered = msg.lower()
    is_daily = (
        "free_tier_requests" in lowered
        or "generaterequestsperday" in lowered.replace(" ", "")
        or "perdayperproject" in lowered.replace(" ", "")
    )
    if is_daily:
        return GeminiQuotaExhaustedError(msg)
    return GeminiUnavailableError(msg)


# 1. The Interface (Protocol)
# This defines the rules. Any class that claims to be an "AIProvider" must implement the 'analyze_text' method.
class AIProvider(Protocol):
    async def analyze_text(self, prompt: str) -> str: ...


# Implementations

# 2. The Cloud Implementation (Gemini)
class GeminiProvider:
    def __init__(self):
        # Configure the Google GenAI Client with our API key
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model_name = settings.LLM_MODEL_NAME
    
    async def analyze_text(self, prompt: str) -> str:
        """
        Sends the prompt to Google's Cloud AI using the new google-genai sdk
        Uses the client.aio (AsyncIO) interfact to avoid blocking
        """
        try:
            # The new SDK exposes async methods under the 'aio' property
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return response.text
        except (ServiceUnavailable, ResourceExhausted) as e:
            # google-api-core path — used by some Gemini code paths
            raise _classify_gemini_error(e) from e
        except Exception as e:
            # google-genai path — the new SDK raises google.genai.errors.*
            # which are not subclasses of the google-api-core exceptions
            # above. Detect by attributes / message instead of catching by
            # class to avoid coupling to SDK internals.
            classified = _classify_gemini_error(e)
            if classified is not None:
                raise classified from e
            raise

# 3.  The local Implementation (Ollama / Llama3)
class LlamaProvider:
    def __init__(self):
        self.url = settings.OLLAMA_BASE_URL
        self.model_name="llama3"
    
    async def analyze_text(self, prompt: str) -> str:
        """
        Sends prompt to a local Ollama instance via HTTP
        """
        payload = {"model": self.model_name, "prompt": prompt, "stream": False}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.url, json=payload, timeout=60.0)
                response.raise_for_status()
                data = response.json()
                return data.get("response", "No response from Ollama")
            except Exception as e:
                return f"Llama Error: {str(e)}"


# 4. The Factory
# This function acts as the switchboard.
def get_ai_provider() -> AIProvider:
    if settings.AI_MODE == "local":
        return LlamaProvider()
    else:
        return GeminiProvider()


