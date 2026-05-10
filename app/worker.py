# ------------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Celery Worker Configuration and Task Definitions
# ------------------------------------------------------------------------------------------------------------------------------------------------

from celery import Celery
import asyncio
from app.config import settings
from app.ai import get_ai_provider

# 1. Initialize the Celery App
# We name it 'smartats_worker' and pass the broker connection string
celery_app = Celery(
    "smartats_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# 2. Configure Celery
# We tell it to serialize data using JSON, which is standard and safe.
celery_app.conf.update(
    task_serializer = "json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# 3. Define the Task
# The @celery_app.task decorator turns this function into a background job.
@celery_app.task(name="analyze_resume_task")
def analyze_resume_task(text_content: str) -> dict:
    """
    Background task to perform AI analysis on resume text.
    This function runs in a separate process (Worker), not the API.
    """
    print(f"Worker: Received text pf length {len(text_content)}. Starting analysis...")

    # We need to run the Async AI logic inside this Sync wrapper.
    try:
        # Get the configured provider (Gemini or Llama)
        ai_provider = get_ai_provider()

        prompt = f"You are an expert tech recruiter. Critique the following resume text. Be concise. Resume: {text_content}"

        # Execute the asynnc function synchronously
        # This blocks he WORKER, but that's okay (it doesn't block the API)
        response =  asyncio.run(ai_provider.analyze_text(prompt))

        print("Worker: Analysis complete.")
        return {"status": "comleted", "critique": response}
    except Exception as e:
        print(f"Worker Error: {str(e)}")
        return {"status": "failed", "error": str(e)}

