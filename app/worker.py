# ---------------------------------------------------------------------------
# Purpose: Celery Worker Configuration and Task Definitions
# ---------------------------------------------------------------------------

import asyncio
import io
import json

import pypdf
from celery import Celery
from sqlmodel import Session

from app.ai import get_ai_provider, GeminiUnavailableError
from app.config import settings
from app.database import engine
from app.models import Application, JobListing
from app.utils import get_s3_client

# 1. Initialize Celery
celery_app = Celery(
    "smartats_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# 2. Configure Security & Serialization
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


# ---------------------------------------------------------------------------
# Shared helper — runs AI analysis and writes the result back to the DB.
# Called by both analyze_resume_task and rescore_application_task.
# ---------------------------------------------------------------------------
def _analyze_and_save(text_content: str, application_id: int) -> dict:
    # Fetch the job this application belongs to so scoring is role-specific
    with Session(engine) as session:
        app_record = session.get(Application, application_id)
        if not app_record:
            raise ValueError(f"Application {application_id} not found")
        job = session.get(JobListing, app_record.job_id)
        if not job:
            raise ValueError(f"Job {app_record.job_id} not found")
        job_title       = job.title
        job_description = job.description
        job_skills      = job.skills
        job_location    = job.location

    ai_provider = get_ai_provider()

    prompt = f"""
    You are an expert tech recruiter. Score the resume below against the specific job posting.

    Job Details:
    - Title: {job_title}
    - Description: {job_description}
    - Required Skills: {job_skills}
    - Location: {job_location}

    Return a strict JSON response:
    {{
        "score": (integer 0-100, reflecting how well this candidate fits this specific role),
        "critique": (string summary of the candidate's strengths and gaps relative to this role)
    }}

    Resume:
    {text_content}
    """

    raw_response = asyncio.run(ai_provider.analyze_text(prompt))
    print(f"Worker: Raw AI response for App {application_id}: {raw_response!r}")

    cleaned = raw_response.replace("```json", "").replace("```", "").strip()
    try:
        analysis_data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI returned non-JSON response: {cleaned!r}") from e

    score = analysis_data.get("score", 0)
    critique = analysis_data.get("critique", "No critique provided.")

    with Session(engine) as session:
        app_record = session.get(Application, application_id)
        if app_record:
            app_record.ai_score = score
            app_record.ai_critique = critique
            app_record.status = "processed"
            session.add(app_record)
            session.commit()
            print(f"Worker: Database updated for App ID {application_id}")
        else:
            print(f"Worker: Error - App ID {application_id} not found!")

    return {"status": "success", "score": score}


# ---------------------------------------------------------------------------
# Task 1 — initial scoring (text already extracted at upload time)
# ---------------------------------------------------------------------------
@celery_app.task(
    name="analyze_resume_task",
    autoretry_for=(GeminiUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=4,
)
def analyze_resume_task(text_content: str, application_id: int) -> dict:
    print(f"Worker: Processing App ID {application_id}...")
    try:
        return _analyze_and_save(text_content, application_id)
    except Exception as e:
        print(f"Worker Error: {str(e)}")
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Task 2 — re-scoring after a job edit (fetches PDF from MinIO, re-extracts)
# ---------------------------------------------------------------------------
@celery_app.task(
    name="rescore_application_task",
    autoretry_for=(GeminiUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=4,
)
def rescore_application_task(application_id: int) -> dict:
    print(f"Worker: Re-scoring App ID {application_id}...")
    try:
        # A. Get the resume path from the DB
        with Session(engine) as session:
            app_record = session.get(Application, application_id)
            if not app_record:
                return {"status": "error", "message": f"App ID {application_id} not found"}
            # resume_url is stored as "/download/{s3_key}"
            s3_key = app_record.resume_url.split("/download/")[-1]

        # B. Download PDF directly from MinIO
        s3 = get_s3_client()
        obj = s3.get_object(Bucket=settings.MINIO_BUCKET_NAME, Key=s3_key)
        pdf_bytes = obj["Body"].read()

        # C. Re-extract text
        pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_content = "".join(page.extract_text() or "" for page in pdf_reader.pages)

        # D. Re-run AI analysis and save
        return _analyze_and_save(text_content, application_id)

    except Exception as e:
        print(f"Worker Error (rescore App {application_id}): {str(e)}")
        return {"status": "error", "message": str(e)}
