# -------------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: The Abstract AI Strategy & Concrete Implementations
# -------------------------------------------------------------------------------------------------------------------------------------------------

from typing import Protocol, Any
from google import genai
import httpx
from app.config import settings

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
        self.model_name = "gemini-2.5-flash"
    
    async def analyze_text(self, prompt: str) -> str:
        """
        Sends the prompt to Google's Cloud AI using the new google-genai sdk
        Uses the client.aio (AsyncIO) interfact to avoid blocking
        """
        try:
            # The new SDK exposes asuync methods under the 'aio' property
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return response.text
        except Exception as e:
            return f"Gemini Error: {str(e)}"

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


