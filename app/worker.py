# ---------------------------------------------------------------------------
# Purpose: Celery Worker Configuration and Task Definitions
# ---------------------------------------------------------------------------

import asyncio
import json

from celery import Celery
from sqlmodel import Session

from app.ai import get_ai_provider

# Local Imports
from app.config import settings
from app.database import engine  # Needed to open a DB connection
from app.models import Application  # Needed to update the row

# 1. Initialize Celery
# We connect to Redis (Broker) to get tasks and Redis (Backend) to store results.
celery_app = Celery(
    "smartats_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# 2. Configure Security & Serialization
# We strictly enforce JSON to prevent code execution attacks (pickle).
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


@celery_app.task(name="analyze_resume_task")
def analyze_resume_task(text_content: str, application_id: int) -> dict:
    """
    The "Brain" of the operation.
    1. Analyzes the text using AI.
    2. Writes the result back to the Postgres Database.
    """
    print(f"Worker: Processing App ID {application_id}...")

    try:
        # A. Run AI Analysis
        ai_provider = get_ai_provider()

        # We craft a "System Prompt" that forces the AI to be a computer program
        # rather than a chat bot. We need strict JSON for our code to work.
        prompt = f"""
        You are an expert tech recruiter. Analyze the resume below.
        Return a strict JSON response:
        {{
            "score": (integer 0-100),
            "critique": (string summary)
        }}
        Resume: {text_content[:3000]}
        """

        # THE SYNC/ASYNC BRIDGE
        # Celery workers are synchronous by default. Our AI Provider is asynchronous.
        # asyncio.run() creates a temporary event loop just for this one function call.

        raw_response = asyncio.run(ai_provider.analyze_text(prompt))

        # B. Parse AI Response (The "Sanitization" Step)
        # LLMs often wrap JSON in Markdown code blocks (```json ... ```).
        # We must strip these out before parsing, or the worker will crash.
        cleaned_response = (
            raw_response.replace("```json", "").replace("```", "").strip()
        )
        analysis_data = json.loads(cleaned_response)

        # Extract data with safety defaults
        score = analysis_data.get("score", 0)
        critique = analysis_data.get("critique", "No critique provided.")

        # C. Update Database (The Feedback Loop)
        # This is what turns "Pending" into "Processed" on the Dashboard.
        with Session(engine) as session:
            # 1. Fetch the application by ID
            app_record = session.get(Application, application_id)

            if app_record:
                # 2. Update the empty fields
                app_record.ai_score = score
                app_record.ai_critique = critique
                app_record.status = "processed"

                # 3. Commit the changes to Postgres
                session.add(app_record)
                session.commit()
                print(f"Worker: Database updated for App ID {application_id}")
            else:
                print(f"Worker: Error - App ID {application_id} not found!")

        return {"status": "success", "score": score}

    except Exception as e:
        print(f"Worker Error: {str(e)}")
        # In a real production app, we would update the DB status to 'failed' here.
        return {"status": "error", "message": str(e)}


