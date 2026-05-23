# ---------------------------------------------------------------------------
# Purpose: Celery Worker Configuration and Task Definitions
# ---------------------------------------------------------------------------

import asyncio
import io
import json

import pypdf
from celery import Celery, Task
from langchain.text_splitter import RecursiveCharacterTextSplitter
from sqlmodel import Session, select

from app.ai import get_ai_provider, GeminiUnavailableError
from app.config import settings
from app.database import engine
from app.email import send_application_scored_email
from app.embeddings import EmbeddingError, embed_texts
from app.models import Application, JobListing, ResumeEmbedding
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
# Task base class — failure tracking
# ---------------------------------------------------------------------------
# When a Celery task exhausts its `max_retries`, this on_failure hook fires.
# It updates the corresponding error column on the Application row and sets
# the status to "failed" so the dashboard can surface it and offer a retry.
#
# Subclasses set `error_column` to the field they should populate.
# All tasks accept `application_id` as the last positional argument or as a
# keyword arg, so we resolve it consistently.

class TaskWithFailureTracking(Task):
    error_column: str = "scoring_error"  # subclasses override

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        application_id = kwargs.get("application_id")
        if application_id is None and args:
            application_id = args[-1]
        if application_id is None:
            print(f"Worker: on_failure could not resolve application_id (args={args}, kwargs={kwargs})")
            return

        with Session(engine) as session:
            app_record = session.get(Application, application_id)
            if app_record:
                setattr(app_record, self.error_column, str(exc))
                app_record.status = "failed"
                session.add(app_record)
                session.commit()
                print(f"Worker: marked App {application_id} as failed ({self.error_column}={exc})")


class ScoringTask(TaskWithFailureTracking):
    error_column = "scoring_error"


class EmbeddingTask(TaskWithFailureTracking):
    error_column = "embedding_error"


# ---------------------------------------------------------------------------
# Shared helper — runs AI analysis and writes the result back to the DB.
# Called by both analyze_resume_task and rescore_application_task.
#
# Errors are RAISED, not caught. The task wrapper lets them propagate so
# Celery's autoretry and on_failure mechanisms work correctly.
# ---------------------------------------------------------------------------
def _analyze_and_save(text_content: str, application_id: int, is_rescore: bool = False) -> dict:
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

    candidate_email = None
    candidate_name = None

    with Session(engine) as session:
        app_record = session.get(Application, application_id)
        if app_record:
            app_record.ai_score = score
            app_record.ai_critique = critique
            app_record.status = "processed"
            # Clear any previous scoring failure now that we have succeeded
            app_record.scoring_error = None
            candidate_email = app_record.candidate_email
            candidate_name = app_record.candidate_name
            session.add(app_record)
            session.commit()
            print(f"Worker: Database updated for App ID {application_id}")
        else:
            print(f"Worker: Error - App ID {application_id} not found!")

    if candidate_email:
        try:
            send_application_scored_email(candidate_email, candidate_name, job_title, is_rescore=is_rescore)
        except Exception as e:
            print(f"Worker: Failed to send scored email to {candidate_email}: {e}")

    return {"status": "success", "score": score}


# ---------------------------------------------------------------------------
# Task 1 — initial scoring (text already extracted at upload time)
# ---------------------------------------------------------------------------
@celery_app.task(
    base=ScoringTask,
    name="analyze_resume_task",
    autoretry_for=(GeminiUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=4,
)
def analyze_resume_task(text_content: str, application_id: int) -> dict:
    print(f"Worker: Processing App ID {application_id}...")
    return _analyze_and_save(text_content, application_id)


# ---------------------------------------------------------------------------
# Task 2 — re-scoring after a job edit (fetches PDF from MinIO, re-extracts)
# ---------------------------------------------------------------------------
@celery_app.task(
    base=ScoringTask,
    name="rescore_application_task",
    autoretry_for=(GeminiUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=4,
)
def rescore_application_task(application_id: int) -> dict:
    print(f"Worker: Re-scoring App ID {application_id}...")

    # A. Get the resume path from the DB
    with Session(engine) as session:
        app_record = session.get(Application, application_id)
        if not app_record:
            raise ValueError(f"App ID {application_id} not found")
        s3_key = app_record.resume_url.split("/download/")[-1]

    # B. Download PDF directly from MinIO
    s3 = get_s3_client()
    obj = s3.get_object(Bucket=settings.MINIO_BUCKET_NAME, Key=s3_key)
    pdf_bytes = obj["Body"].read()

    # C. Re-extract text
    pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    text_content = "".join(page.extract_text() or "" for page in pdf_reader.pages)

    # D. Re-run AI analysis and save
    return _analyze_and_save(text_content, application_id, is_rescore=True)


# ---------------------------------------------------------------------------
# Task 3 — embed resume into pgvector for semantic search / RAG (Phase 1)
# ---------------------------------------------------------------------------
# Chunks the resume text via RecursiveCharacterTextSplitter, batch-embeds via
# Gemini's gemini-embedding-001, and stores one row per chunk in
# resume_embedding. Idempotent — re-running the task replaces any existing
# chunks for the application.
@celery_app.task(
    base=EmbeddingTask,
    name="embed_resume_task",
    autoretry_for=(EmbeddingError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=4,
)
def embed_resume_task(text_content: str, application_id: int) -> dict:
    print(f"Worker: Embedding App ID {application_id}...")

    if len(text_content.strip()) < 50:
        print(f"Worker: Skipping App {application_id} embedding — text too short")
        return {"status": "skipped", "reason": "text too short"}

    # 1. Idempotency: clear any existing chunks for this application
    with Session(engine) as session:
        existing = session.exec(
            select(ResumeEmbedding).where(ResumeEmbedding.application_id == application_id)
        ).all()
        for row in existing:
            session.delete(row)
        if existing:
            session.commit()
            print(f"Worker: cleared {len(existing)} existing chunks for App {application_id}")

    # 2. Chunk the resume text
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.RESUME_CHUNK_SIZE,
        chunk_overlap=settings.RESUME_CHUNK_OVERLAP,
    )
    chunks = splitter.split_text(text_content)
    print(f"Worker: split App {application_id} resume into {len(chunks)} chunks")

    # 3. Batch-embed all chunks in a single API call
    # Raises EmbeddingError on Gemini failure → triggers Celery retry
    vectors = embed_texts(chunks)

    # 4. Insert all chunks
    with Session(engine) as session:
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            session.add(ResumeEmbedding(
                application_id=application_id,
                chunk_index=i,
                chunk_text=chunk,
                embedding=vector,
            ))
        # Clear any previous embedding failure now that we have succeeded
        app_record = session.get(Application, application_id)
        if app_record:
            app_record.embedding_error = None
            # If both errors are now clear and status was failed, restore it
            if app_record.status == "failed" and app_record.scoring_error is None:
                app_record.status = "processed" if app_record.ai_score else "pending"
            session.add(app_record)
        session.commit()
        print(f"Worker: stored {len(chunks)} embeddings for App {application_id}")

    return {"status": "success", "chunks": len(chunks)}
