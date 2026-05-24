# ---------------------------------------------------------------------------
# Purpose: The Entry Point for the SmartATS API
# ---------------------------------------------------------------------------

import io
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, List

import pypdf

# Background Task Imports
from celery.result import AsyncResult  # To check status
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlmodel import Session, select

from app.ai import AIProvider, GeminiUnavailableError, get_ai_provider
from app.auth import auth_router, get_current_user
from app.config import settings
from app.embeddings import EmbeddingError, embed_query_cached
from app.limiter import get_user_key, limiter

# Import our local modules
from app.database import create_db_and_tables, get_session

# CRITICAL: We import ApplicationSubmit here to handle the full frontend payload
from app.models import (
    AnalysisRequest,
    Application,
    ApplicationSubmit,
    CrossJobMatch,
    CrossJobMatchResult,
    JobListing,
    JobListingUpdate,
    SearchQuery,
    SearchResponse,
    SearchResult,
    User,
)
from app.utils import get_s3_client, init_storage
from sqlalchemy.exc import IntegrityError

from app.worker import (
    analyze_resume_task,
    celery_app,
    embed_job_task,
    embed_resume_task,
    match_jobs_task,
    rescore_application_task,
)
from celery import chain


# 1. LIFESPAN CONTEXT MANAGER
# This is the "Startup Sequence" of our application.
# Before the first user connects, we ensure the DB tables exist and MinIO is ready.
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Startup: Creating database tables...")
    create_db_and_tables()
    print("Startup: Checking Object Storage...")
    init_storage()
    yield
    print("Shutdown: Cleaning up resources...")


app = FastAPI(
    title="SmartATS",
    description="An AI-Powered Applicant Tracking System (ATS)",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 2. MOUNT STATIC FILES
# This tells FastAPI: "If a user asks for /static/css/style.css, look in the app/static folder."
# This effectively turns our API into a Web Server for the frontend files.
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# 3. PAGE ROUTES (Serving HTML)
# These endpoints just return the raw HTML files.
# The JavaScript inside those files will call our JSON APIs later.
_NO_CACHE = {"Cache-Control": "no-store"}

@app.get("/", include_in_schema=False)
async def read_root():
    """Serve the candidate-facing job board (index.html)."""
    return FileResponse("app/static/index.html", headers=_NO_CACHE)


@app.get("/dashboard", include_in_schema=False)
async def read_dashboard():
    """Serve the recruiter dashboard (dashboard.html). Requires a valid JWT stored in localStorage."""
    return FileResponse("app/static/dashboard.html", headers=_NO_CACHE)


@app.get("/login", include_in_schema=False)
async def read_login():
    """Serve the recruiter login page (login.html)."""
    return FileResponse("app/static/login.html", headers=_NO_CACHE)


@app.get("/register", include_in_schema=False)
async def read_register():
    """Serve the recruiter registration page (register.html)."""
    return FileResponse("app/static/register.html", headers=_NO_CACHE)


@app.get("/verify-email", include_in_schema=False)
async def read_verify_email():
    """
    Serve the email-verification landing page (verify-email.html).

    The page reads the `?token=` query parameter from the URL, then calls
    `POST /verify-email` via JavaScript to complete verification — no raw
    JSON is ever shown to the user.
    """
    return FileResponse("app/static/verify-email.html", headers=_NO_CACHE)


@app.get("/reset-password", include_in_schema=False)
async def read_reset_password():
    """
    Serve the password-reset page (reset-password.html).

    The page reads the `?token=` query parameter from the URL and presents
    a new-password form. On submission it calls `POST /reset-password` via
    JavaScript.
    """
    return FileResponse("app/static/reset-password.html", headers=_NO_CACHE)


# --- 4. REGISTER MODULES ---
# We attach the Auth routes (/token, /register) defined in auth.py
app.include_router(auth_router)


# 5. DEPENDENCY INJECTION CONFIGURATION
# This makes our path operations cleaner and easier to test.
SessionDep = Annotated[Session, Depends(get_session)]

# AI Dependency: Injects the correct AI class based on settings (Gemini/Llama)
AIDep = Annotated[AIProvider, Depends(get_ai_provider)]


@app.get("/health", tags=["Infra"])
async def health_check():
    """
    Liveness probe for load balancers and container orchestrators.

    Returns a simple JSON payload confirming the API process is running.
    Does **not** check downstream dependencies (DB, Redis, MinIO) — use this
    only to verify the web container itself is alive.
    """
    return {"status": "ok", "message": "SmartATS is ready to serve 🚀"}


@app.get("/health/ai", tags=["Infra"])
@limiter.limit("2/minute", key_func=get_user_key)
@limiter.limit("2/minute", key_func=get_remote_address)
async def check_ai_health(request: Request, ai: AIDep):
    """
    Probe the configured AI provider (Gemini or Ollama) with a minimal prompt.

    Sends a one-word prompt and checks for a coherent response. Always returns
    HTTP 200 with a `status` field so the dashboard can display a clear human-
    readable message regardless of outcome:

    - `"ok"` — provider responded correctly
    - `"unavailable"` — transient 503/429 from Gemini (high demand); retry later
    - `"error"` — unexpected failure (bad API key, network issue, etc.)

    This endpoint exists so that demo reviewers can distinguish between an
    application bug and a Gemini outage.
    """
    try:
        response = await ai.analyze_text("Reply with the single word: OK")
        return {
            "status": "ok",
            "provider": settings.AI_MODE,
            "message": "AI provider is online and responding correctly.",
            "response": response,
        }
    except GeminiUnavailableError:
        return {
            "status": "unavailable",
            "provider": settings.AI_MODE,
            "message": "Gemini is experiencing high demand and is temporarily unavailable. This is a Google-side issue — please try again in a few minutes.",
        }
    except Exception as e:
        return {
            "status": "error",
            "provider": settings.AI_MODE,
            "message": f"AI provider returned an unexpected error: {e}",
        }


# --- JOB ROUTES ---


# Create a Job (POST)
# SECURITY: Notice 'current_user' dependency.
# If the user does not have a valid Token, FastAPI rejects this request (401).
@app.post("/jobs", response_model=JobListing, tags=["Jobs"])
def create_job(
    job: JobListing,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Create a new job listing. Protected — requires a valid recruiter JWT.

    Automatically sets `owner_id` to the authenticated recruiter so they can
    only manage their own postings.

    **Errors:**
    - `401` – missing or invalid token
    - `422` – validation failure (e.g. title too short, negative salary)
    """
    job.owner_id = current_user.id
    session.add(job)
    session.commit()
    session.refresh(job)

    # Dispatch job embedding for cross-job matching (Phase 3).
    # Embedding runs asynchronously and is independent of job creation success.
    embed_job_task.delay(job_id=job.id)

    return job


# List all Jobs (GET) — PUBLIC for candidates
@app.get("/jobs", response_model=List[JobListing], tags=["Jobs"])
def list_jobs(session: SessionDep):
    """
    List all job postings. Public — no authentication required.

    Used by the candidate-facing job board (`/`) to populate the list of
    open positions that candidates can apply to.
    """
    return session.exec(select(JobListing)).all()


@app.delete("/jobs/{job_id}", status_code=204, tags=["Jobs"])
def delete_job(
    job_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Permanently delete a job listing, all its applications, and their resume
    PDFs from MinIO storage. Protected.

    Order of operations:
    1. Collect the MinIO S3 key from every application before deletion.
    2. Delete the job — the CASCADE constraint removes all Application rows.
    3. Delete each resume PDF from MinIO. This step is best-effort: a MinIO
       failure is logged but does not roll back the DB deletion, since the
       job and applications are already gone and retrying the DB delete would
       be worse than leaving an orphaned file.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the job belongs to a different recruiter
    - `404` – no job found with the given ID
    """
    job = session.get(JobListing, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this job.")

    # Collect S3 keys before the CASCADE wipes the application rows
    applications = session.exec(
        select(Application).where(Application.job_id == job_id)
    ).all()
    s3_keys = [
        app.resume_url.split("/download/")[-1]
        for app in applications
        if app.resume_url
    ]

    # Delete job — CASCADE removes all Application rows automatically
    session.delete(job)
    session.commit()

    # Delete resume PDFs from MinIO (best-effort — logged on failure)
    if s3_keys:
        s3 = get_s3_client()
        for key in s3_keys:
            try:
                s3.delete_object(Bucket=settings.MINIO_BUCKET_NAME, Key=key)
            except Exception as e:
                print(f"Warning: failed to delete resume {key} from MinIO: {e}")


@app.patch("/jobs/{job_id}", response_model=JobListing, tags=["Jobs"])
def update_job(
    job_id: int,
    updates: JobListingUpdate,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Partially update a job listing. Protected. Only provided fields are changed.

    If any of `title`, `description`, `skills`, or `location` change, all
    existing applications for this job are automatically reset to `pending`
    and re-queued for AI re-scoring, because the new details may affect how
    well a resume matches. Changing only `salary_range` does **not** trigger
    re-scoring as salary is not considered by the AI.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the job belongs to a different recruiter
    - `404` – no job found with the given ID
    - `422` – validation failure on updated fields
    """
    job = session.get(JobListing, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this job.")

    patch = updates.model_dump(exclude_unset=True)
    changed_fields = set(patch.keys())
    for key, value in patch.items():
        setattr(job, key, value)
    session.add(job)
    session.commit()
    session.refresh(job)

    # Re-score and re-embed only when fields that affect candidate fit actually
    # change. Salary is a business constraint invisible to the AI — no rescore
    # or re-embedding needed. This same set is used for both rescoring existing
    # applications AND for re-embedding the job description for cross-job
    # matching (Phase 3) — they share the same trigger semantics.
    SCORING_RELEVANT_FIELDS = {"title", "description", "skills", "location"}
    if changed_fields & SCORING_RELEVANT_FIELDS:
        applications = session.exec(select(Application).where(Application.job_id == job_id)).all()
        for app_record in applications:
            app_record.status = "pending"
            app_record.ai_score = 0
            app_record.ai_critique = None
            session.add(app_record)
        session.commit()
        for app_record in applications:
            rescore_application_task.delay(app_record.id)

        # Re-embed the job description so cross-job matching reflects the edit.
        # Existing CrossJobMatch rows pointing to this job remain valid until
        # the next match_jobs_task run for each candidate; recruiters can
        # trigger a bulk recheck if they want immediate freshness.
        embed_job_task.delay(job_id=job.id)

    return job


# List only the logged-in recruiter's jobs — PROTECTED
@app.get("/my-jobs", response_model=List[JobListing], tags=["Jobs"])
def list_my_jobs(
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    List only the job postings that belong to the authenticated recruiter.

    Used by the recruiter dashboard to show the left-hand jobs panel.
    Unlike `GET /jobs`, this endpoint filters by `owner_id` so recruiters
    only see their own postings.

    **Errors:**
    - `401` – missing or invalid token
    """
    return session.exec(
        select(JobListing).where(JobListing.owner_id == current_user.id)
    ).all()


# --- AI ANALYSIS ROUTES (Direct Test) ---
@app.post("/analyze", tags=["AI"])
async def analyze_resume_text(request: AnalysisRequest, ai: AIDep):
    """
    Debug endpoint — run the AI scorer against raw text without touching the DB.

    Scores the supplied text against a generic Senior Developer role and
    returns the raw AI response string. Useful for verifying the active AI
    provider (Gemini or Ollama) and tuning the prompt without creating an
    application record.

    This endpoint is **not** used by the normal application flow; use
    `POST /process` for real submissions.
    """
    prompt = f"""
    You are an expert tech recruiter. Analyze the following resume text against a generic Senior Developer role.
    
    Return your response in this exact JSON format:
    {{
        "score": (integer 0-100),
        "critique": (string, concise summary of gaps and strengths)
    }}
    
    Resume Text:
    {request.text}
    """
    analysis = await ai.analyze_text(prompt)
    return {"analysis": analysis}


# --- FILE UPLOAD ROUTES ---
@app.post("/upload", tags=["Applications"])
@limiter.limit("10/minute")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Upload a candidate's resume PDF. Public — no authentication required.

    Performs two validation layers before storing the file:
    1. **MIME type check** — rejects anything that is not `application/pdf`.
    2. **Magic-byte check** — reads the first 4 bytes and rejects files that
       do not start with `%PDF`, catching renamed non-PDF files.

    On success, extracts the full resume text (used later by the AI scorer),
    stores the PDF in MinIO under a UUID key to prevent filename collisions,
    and returns the internal download URL (`/download/{s3_key}`) for the
    frontend to include in the application submission payload.

    **Errors:**
    - `400` – wrong MIME type or corrupt/non-PDF file
    - `500` – text extraction failed or MinIO storage error
    """

    # 1. Validation: Check File Extension
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400, detail="Invalid file type. Only PDFs are allowed."
        )

    # 2. Validation: Magic Numbers (Check file signature)
    header = await file.read(4)
    if header != b"%PDF":
        raise HTTPException(status_code=400, detail="Corrupt or invalid PDF file.")

    # CRITICAL: Reset cursor so we can read the file again
    await file.seek(0)

    # 3. Read content into memory
    content = await file.read()

    # 4. Text Extraction (For the AI)
    try:
        pdf_reader = pypdf.PdfReader(io.BytesIO(content))
        extracted_text = ""
        for page in pdf_reader.pages:
            extracted_text += page.extract_text() or ""
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract text: {str(e)}")

    # 5. Generate Unique Key (Prevent filename collisions)
    file_id = str(uuid.uuid4())
    s3_key = f"{file_id}.pdf"

    # 6. Upload to MinIO (The Vault)
    try:
        s3 = get_s3_client()
        s3.put_object(
            Bucket=settings.MINIO_BUCKET_NAME,
            Key=s3_key,
            Body=content,
            ContentType="application/pdf",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage Error: {str(e)}")

    # 7. Return Metadata
    # Use the FastAPI proxy path — MinIO is internal-only and unreachable by browsers.
    file_url = f"/download/{s3_key}"

    return {
        "file_id": file_id,
        "filename": file.filename,
        "s3_key": s3_key,
        "extracted_text_preview": extracted_text[:200] + "...",
        "file_url": file_url,  # Needed for the frontend!
    }


@app.get("/download/{s3_key}", tags=["Applications"])
async def download_resume(s3_key: str):
    """
    Stream a stored resume PDF to the browser. Public — no authentication required.

    Acts as a reverse proxy between the browser and MinIO. MinIO runs on the
    internal Docker network and is not reachable directly by browsers, so all
    PDF downloads are routed through this endpoint.

    The PDF is streamed (not buffered) to avoid loading large files fully into
    memory, and served with `Content-Disposition: inline` so the browser
    renders it in-tab rather than forcing a download.

    **Errors:**
    - `404` – no object found in MinIO for the given key
    """
    try:
        s3 = get_s3_client()
        obj = s3.get_object(Bucket=settings.MINIO_BUCKET_NAME, Key=s3_key)
        return StreamingResponse(
            obj["Body"],
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename={s3_key}"},
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Resume not found: {str(e)}")


# --- APPLICATION ROUTES ---
@app.get("/applications/{job_id}", response_model=List[Application], tags=["Applications"])
def get_applications(
    job_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Retrieve all applications for a specific job, ranked by AI score. Protected.

    Returns the full application list sorted by `ai_score` descending so the
    best-matching candidates appear at the top of the recruiter dashboard.
    Applications still being processed by the worker have `status = "pending"`
    and `ai_score = 0`; the dashboard polls and refreshes to pick up updates.

    **Errors:**
    - `401` – missing or invalid token
    """
    statement = (
        select(Application)
        .where(Application.job_id == job_id)
        .order_by(Application.ai_score.desc())
    )
    results = session.exec(statement).all()
    return results


# ---------------------------------------------------------------------------
# Semantic search SQL (Phase 2)
# ---------------------------------------------------------------------------
# CTE ranks every resume chunk by similarity to the query within each
# candidate, restricted to jobs owned by the current recruiter. The outer
# query keeps only the best chunk per candidate (rank = 1), applies the
# similarity threshold, and paginates.
#
# Full walkthrough of every clause in docs/ai-features/phase-2-semantic-search.md
SEARCH_SQL = """
WITH ranked_chunks AS (
    SELECT
        re.application_id,
        re.chunk_text,
        1 - (re.embedding <=> CAST(:query_vector AS vector)) AS similarity,
        ROW_NUMBER() OVER (
            PARTITION BY re.application_id
            ORDER BY re.embedding <=> CAST(:query_vector AS vector)
        ) AS rank_within_candidate
    FROM resumeembedding re
    JOIN application a ON a.id = re.application_id
    JOIN joblisting j ON j.id = a.job_id
    WHERE j.owner_id = :owner_id
)
SELECT
    a.id AS application_id,
    a.candidate_name,
    a.candidate_email,
    a.resume_url,
    j.title AS job_title,
    rc.chunk_text AS best_match_chunk,
    rc.similarity
FROM ranked_chunks rc
JOIN application a ON a.id = rc.application_id
JOIN joblisting j ON j.id = a.job_id
WHERE rc.rank_within_candidate = 1
  AND rc.similarity >= :min_similarity
ORDER BY rc.similarity DESC
LIMIT :limit OFFSET :offset
"""

SEARCH_MIN_SIMILARITY = 0.6


@app.post("/search/candidates", response_model=SearchResponse, tags=["AI"])
@limiter.limit("10/minute", key_func=get_user_key)
def search_candidates(
    request: Request,
    payload: SearchQuery,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Semantic search across the authenticated recruiter's applicant pool. Protected.

    Embeds the query text, then runs a vector-similarity search over
    `resume_embedding`, restricted to applications for jobs owned by the
    current recruiter. Returns one row per candidate with the resume chunk
    that matched best, ranked by cosine similarity (≥ 0.6), paginated.

    Query embeddings are cached in Redis for one hour to absorb repeated
    searches without hitting Gemini.

    **Errors:**
    - `401` – missing or invalid token
    - `429` – rate limit hit (10 searches/minute per user)
    - `503` – embedding provider unavailable (transient — retry shortly)
    """
    try:
        query_vector = embed_query_cached(payload.query)
    except EmbeddingError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Search temporarily unavailable: {e}",
        )

    rows = session.execute(
        text(SEARCH_SQL),
        {
            "query_vector": str(query_vector),
            "owner_id": current_user.id,
            "min_similarity": SEARCH_MIN_SIMILARITY,
            "limit": payload.limit,
            "offset": payload.offset,
        },
    ).fetchall()

    results = [
        SearchResult(
            application_id=row.application_id,
            candidate_name=row.candidate_name,
            candidate_email=row.candidate_email,
            resume_url=row.resume_url,
            job_title=row.job_title,
            best_match_chunk=row.best_match_chunk,
            similarity=float(row.similarity),
        )
        for row in rows
    ]

    return SearchResponse(
        results=results,
        total_returned=len(results),
        has_more=len(results) == payload.limit,
    )


@app.post("/applications/{application_id}/retry", tags=["Applications"])
def retry_application(
    application_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Re-dispatch failed Celery tasks for an application. Protected.

    When a task (scoring, embedding, or matching) exhausts its Celery retries,
    the corresponding `on_failure` hook records the error in `scoring_error`,
    `embedding_error`, or `matching_error` and sets the application status to
    `"failed"`. This endpoint lets the recruiter re-trigger the failed task(s)
    from the dashboard:

    1. Looks at the three error columns to determine what failed
    2. Fetches the stored PDF from MinIO and re-extracts text if a scoring or
       embedding retry is needed (we don't persist the extracted text)
    3. Re-dispatches `analyze_resume_task`, `embed_resume_task`, and/or
       `match_jobs_task` as needed
    4. Clears the corresponding error columns and resets status to `"pending"`

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the parent job belongs to a different recruiter
    - `404` – application not found
    - `400` – application is not in a failed state (nothing to retry)
    """
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    # Confirm the recruiter owns the parent job
    job = session.get(JobListing, app_record.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Parent job not found.")
    if job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    if not (app_record.scoring_error or app_record.embedding_error or app_record.matching_error):
        raise HTTPException(status_code=400, detail="Nothing to retry — no failed tasks.")

    # If scoring or embedding failed, we need the resume text. Re-extract from
    # MinIO since we don't persist the extracted text. Matching does not need
    # the text (works off the stored embeddings).
    text_content = None
    if app_record.scoring_error or app_record.embedding_error:
        s3_key = app_record.resume_url.split("/download/")[-1]
        try:
            s3 = get_s3_client()
            obj = s3.get_object(Bucket=settings.MINIO_BUCKET_NAME, Key=s3_key)
            pdf_bytes = obj["Body"].read()
            pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            text_content = "".join(page.extract_text() or "" for page in pdf_reader.pages)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not re-read resume from storage: {e}")

    if app_record.scoring_error:
        analyze_resume_task.delay(text_content=text_content, application_id=application_id)
        app_record.scoring_error = None

    if app_record.embedding_error:
        # Re-chain matching after embedding so the dependency is preserved on retry
        chain(
            embed_resume_task.s(text_content=text_content, application_id=application_id),
            match_jobs_task.si(application_id=application_id),
        ).apply_async()
        app_record.embedding_error = None
        # Embedding retry also resolves the matching error since they're chained
        app_record.matching_error = None
    elif app_record.matching_error:
        # Matching alone failed — re-dispatch just the matching task
        match_jobs_task.delay(application_id=application_id)
        app_record.matching_error = None

    app_record.status = "pending"
    session.add(app_record)
    session.commit()

    return {"message": "Retry dispatched", "application_id": application_id}


# ---------------------------------------------------------------------------
# Phase 3 — Cross-job match endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/applications/{application_id}/matches",
    response_model=List[CrossJobMatchResult],
    tags=["AI"],
)
@limiter.limit("60/minute", key_func=get_user_key)
def get_application_matches(
    request: Request,
    application_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    List cross-job match suggestions for a single application. Protected.

    Returns the alternative jobs (within the same recruiter's pool) that the
    candidate's resume also matches well, as computed by `match_jobs_task`.
    Read-only DB query — no Gemini call.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the parent job belongs to a different recruiter
    - `404` – application not found
    """
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    parent_job = session.get(JobListing, app_record.job_id)
    if not parent_job or parent_job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    statement = (
        select(CrossJobMatch, JobListing)
        .join(JobListing, JobListing.id == CrossJobMatch.matched_job_id)
        .where(CrossJobMatch.application_id == application_id)
        .order_by(CrossJobMatch.similarity.desc())
    )
    rows = session.exec(statement).all()

    return [
        CrossJobMatchResult(
            matched_job_id=match.matched_job_id,
            job_title=job.title,
            similarity=match.similarity,
        )
        for match, job in rows
    ]


@app.post("/applications/{application_id}/match-refresh", tags=["AI"])
@limiter.limit("10/minute", key_func=get_user_key)
def refresh_application_matches(
    request: Request,
    application_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Re-compute cross-job matches for a single application. Protected.

    Dispatches `match_jobs_task` for this application. The task is idempotent:
    it deletes existing match rows and inserts fresh ones. Useful after the
    recruiter has posted new jobs and wants to see if existing candidates now
    match the new roles.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the parent job belongs to a different recruiter
    - `404` – application not found
    """
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    parent_job = session.get(JobListing, app_record.job_id)
    if not parent_job or parent_job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    match_jobs_task.delay(application_id=application_id)
    return {"message": "Match refresh dispatched", "application_id": application_id}


@app.post("/matches/refresh-all", tags=["AI"])
@limiter.limit("2/hour", key_func=get_user_key)
def refresh_all_matches(
    request: Request,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Bulk-recompute cross-job matches for every application in the recruiter's
    pool. Protected. Heavy operation — strictly rate-limited (2/hour per user).

    Iterates over every application that belongs to a job owned by the current
    recruiter and dispatches `match_jobs_task` for each. Returns the number of
    tasks queued so the dashboard can show progress feedback.

    **Errors:**
    - `401` – missing or invalid token
    - `429` – rate limit hit (only 2 bulk refreshes per hour)
    """
    statement = (
        select(Application.id)
        .join(JobListing, JobListing.id == Application.job_id)
        .where(JobListing.owner_id == current_user.id)
    )
    application_ids = [row for row in session.exec(statement).all()]

    for app_id in application_ids:
        match_jobs_task.delay(application_id=app_id)

    return {
        "message": "Bulk match refresh dispatched",
        "tasks_queued": len(application_ids),
    }


# --- Processing ROUTES (The Main Workflow) ---
@app.post("/process", tags=["Applications"])
@limiter.limit("10/minute")
async def process_resume(request: Request, payload: ApplicationSubmit, session: SessionDep):
    """
    Submit a candidate application and queue it for AI scoring. Public.

    Orchestrates the full application intake in two steps:

    1. **Persist immediately** — saves the application to Postgres with
       `status = "pending"` before any AI work begins. This guarantees the
       application is never lost even if the worker crashes mid-processing.
    2. **Dispatch asynchronously** — sends the resume text and application ID
       to the Celery worker via Redis. The worker calls Gemini, writes the
       score and critique back to the DB, and emails the candidate.

    The returned `task_id` can be polled via `GET /process/{task_id}` to
    track progress.

    **Errors:**
    - `409` – the candidate has already applied to this job (unique constraint
      on `job_id` + `candidate_email`)
    """

    # A. Create Record in Postgres
    # We save *before* we process. This ensures we don't lose the application
    # even if the AI worker crashes.
    app_record = Application(
        job_id=payload.job_id,
        candidate_name=payload.candidate_name,
        candidate_email=payload.candidate_email,
        resume_url=payload.resume_url,
        status="pending",
    )
    try:
        session.add(app_record)
        session.commit()
        session.refresh(app_record)
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="You have already applied for this posting. Multiple applications are not allowed for the same job.",
        )

    # B. Trigger Workers.
    # Scoring and the embedding-then-match pipeline run in PARALLEL.
    #
    # Scoring: uses Gemini text completion to produce score+critique.
    # Embedding → Matching: a Celery chain (Phase 3) — match_jobs_task needs
    # resume_embedding rows to exist, so it runs strictly AFTER embed_resume_task
    # succeeds. Using .si() (immutable signature) so the matching task does
    # not receive the embedding task's return value as input.
    scoring_task = analyze_resume_task.delay(
        text_content=payload.request.text, application_id=app_record.id
    )
    chain(
        embed_resume_task.s(
            text_content=payload.request.text, application_id=app_record.id
        ),
        match_jobs_task.si(application_id=app_record.id),
    ).apply_async()

    return {
        "message": "Application received",
        "task_id": scoring_task.id,
        "application_id": app_record.id,
    }


@app.get("/process/{task_id}", tags=["Applications"])
async def get_processing_status(task_id: str):
    """
    Poll the status of a Celery AI-scoring task. Public.

    The frontend calls this endpoint repeatedly after submitting an application
    to drive the progress indicator. Queries the Celery result backend (Redis)
    for the task state and returns one of:

    - `Processing...` — task is queued or running
    - `Done` — scoring completed; result contains `score`
    - `Failed` — worker encountered an unrecoverable error; error detail included
    """
    # 1. Fetch the result from Redis Backend
    task_result = AsyncResult(task_id, app=celery_app)
    # 2. Check State
    if task_result.state == "PENDING":
        return {"status": "Processing...", "result": None}
    elif task_result.state == "SUCCESS":
        return {"status": "Done", "result": task_result.result}
    elif task_result.state == "FAILURE":
        return {"status": "Failed", "error": str(task_result.result)}

    return {"status": task_result.state}
