# ---------------------------------------------------------------------------
# Purpose: The Entry Point for the SmartATS API
# ---------------------------------------------------------------------------

import io
import json
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
from datetime import datetime
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.ai import (
    AIProvider,
    GeminiQuotaExhaustedError,
    GeminiUnavailableError,
    _classify_gemini_error,
    get_ai_provider,
)
from app.auth import auth_router, get_current_user
from app.config import settings
from app.chat_history import append_turn, clear_application_chats, load_history
from app.embeddings import EmbeddingError, embed_query_cached
from app.rag import stream_rag_answer
from app.rerank import clear_application_rerank_cache, rerank_parallel
from app.limiter import get_user_key, limiter

# Import our local modules
from app.database import create_db_and_tables, get_session

# CRITICAL: We import ApplicationSubmit here to handle the full frontend payload
from app.models import (
    AnalysisRequest,
    Application,
    ApplicationSubmit,
    ChatRequest,
    Citation,
    CrossApplicantResult,
    CrossJobMatch,
    CrossJobMatchResult,
    CrossMatchInviteRequest,
    DraftSendResponse,
    EmailDraftPublic,
    JobListing,
    JobListingUpdate,
    OutreachEmail,
    ResumeEmbedding,
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
_DEFAULT_SECRET_KEY = "79f0da0c3f80646ad690a44e39706380c40d0d777f5df57ad531c218f86bb270"


def _check_critical_secrets() -> None:
    """
    Refuse to boot if SECRET_KEY is the default value baked into the public
    repo. Without this guard, a forgotten `.env` override in production
    means anyone who reads the repo can forge JWTs for any user — the most
    catastrophic single misconfiguration possible.

    Also warn loudly about other dev defaults that would be unsafe in
    production (Gemini API key, MinIO credentials).
    """
    if settings.SECRET_KEY == _DEFAULT_SECRET_KEY:
        raise RuntimeError(
            "FATAL: SECRET_KEY is set to the public default value baked into the "
            "repository. Anyone reading the repo can forge JWTs and impersonate any "
            "user. Generate a fresh key with `openssl rand -hex 32` and set it in "
            "your .env (or deployment env) under SECRET_KEY=... before starting "
            "the server."
        )
    if settings.GEMINI_API_KEY == "fake-key-for-dev":
        print("WARNING: GEMINI_API_KEY is the dev default. AI features will fail.")
    if settings.MINIO_ACCESS_KEY in ("dummy", "") or settings.MINIO_SECRET_KEY in ("dummy", ""):
        print("WARNING: MINIO credentials are dev defaults. File uploads will fail.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Startup: Checking critical secrets...")
    _check_critical_secrets()
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


# Force browsers to revalidate every /static/* asset on each page load.
# Without this, a redeploy that changes JS/CSS can be invisible to users on
# the old cached version — we hit this in production when adding methods to
# api.js wasn't picked up until users hard-refreshed.
#
# `no-cache` means "you may cache, but always send a conditional request"
# (If-None-Match / If-Modified-Since). The server returns 304 if unchanged,
# so the actual byte cost only kicks in when the file truly changes.
@app.middleware("http")
async def _static_no_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# 3. PAGE ROUTES (Serving HTML)
# These endpoints just return the raw HTML files.
# The JavaScript inside those files will call our JSON APIs later.
_NO_CACHE = {"Cache-Control": "no-store"}

@app.get("/", include_in_schema=False)
async def read_root():
    """Serve the candidate-facing job board (index.html)."""
    return FileResponse("app/static/index.html", headers=_NO_CACHE)


@app.get("/job/{job_id}", include_in_schema=False)
async def read_public_job_page(job_id: int):
    """
    Serve the candidate-facing single-job page (job-details.html). Public —
    no auth required. The page reads the job id from window.location.pathname
    and fetches the job's data via the existing public `GET /jobs` listing.
    Path is singular (/job/{id}) to distinguish from the authenticated
    recruiter route /jobs/{id} that serves the recruiter dashboard view.
    """
    return FileResponse("app/static/job-details.html", headers=_NO_CACHE)


@app.get("/dashboard", include_in_schema=False)
async def read_dashboard():
    """Serve the recruiter jobs-overview page (dashboard.html). Requires a valid JWT stored in localStorage."""
    return FileResponse("app/static/dashboard.html", headers=_NO_CACHE)


@app.get("/jobs/{job_id}", include_in_schema=False)
async def read_job_page(job_id: int):
    """
    Serve the per-job page (job.html). The job_id is read client-side from
    window.location.pathname; auth and ownership are enforced by the JSON
    APIs the page calls. Path param is declared so FastAPI routes correctly.
    """
    return FileResponse("app/static/job.html", headers=_NO_CACHE)


@app.get("/search", include_in_schema=False)
async def read_search_page():
    """Serve the semantic-search page (search.html). Requires a valid JWT."""
    return FileResponse("app/static/search.html", headers=_NO_CACHE)


@app.get("/settings", include_in_schema=False)
async def read_settings_page():
    """Serve the account-settings page (settings.html). Requires a valid JWT."""
    return FileResponse("app/static/settings.html", headers=_NO_CACHE)


@app.get("/assistant", include_in_schema=False)
async def read_assistant_page():
    """
    Serve the recruiter assistant chat page (assistant.html). Requires a
    valid JWT. The chat itself streams via POST /assistant/turn.
    """
    return FileResponse("app/static/assistant.html", headers=_NO_CACHE)


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
    except GeminiQuotaExhaustedError:
        # Daily free-tier quota used up — different from transient high-demand
        # because the wait is hours-to-tomorrow, not minutes.
        return {
            "status": "unavailable",
            "provider": settings.AI_MODE,
            "message": "Gemini's daily free-tier quota has been exhausted. The limit resets in roughly 24 hours, or you can upgrade your Gemini API plan for higher limits.",
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

    # 3. Read content into memory with a hard size cap. Without this a
    # 100 MB PDF would OOM the worker during pypdf parsing OR drive a
    # cost DoS via the embedding pipeline. We cap at 5 MB which fits any
    # plausible resume comfortably (a CV with 50 dense pages is ~2 MB).
    MAX_RESUME_BYTES = 5 * 1024 * 1024
    content = await file.read(MAX_RESUME_BYTES + 1)
    if len(content) > MAX_RESUME_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Resume too large. Maximum allowed size is {MAX_RESUME_BYTES // (1024 * 1024)} MB.",
        )

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
        # Full extracted text — sent to /process so AI scoring and embedding
        # see the entire resume, not a truncated preview. Truncating here
        # caused a real bug where embeddings only saw the first ~200 chars.
        "extracted_text": extracted_text,
        # Short preview kept for display/debug purposes (200 chars + ellipsis)
        "extracted_text_preview": extracted_text[:200] + ("..." if len(extracted_text) > 200 else ""),
        "file_url": file_url,
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
    j.id    AS job_id,
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

# Vector pre-filter threshold for the Phase 5 two-stage flow. Lower than the
# previous one-stage threshold (0.7) because the LLM rerank does the real
# precision filtering downstream — we want the pre-filter to surface more
# candidates so the LLM has a richer set to choose from.
SEARCH_VECTOR_MIN_SIMILARITY = 0.55

# LLM rerank score (0–100) below which a candidate is not surfaced to the
# recruiter. Calibrated independently of the vector threshold — they have
# different semantics.
SEARCH_LLM_MIN_SCORE = 60

# Top-K candidates to pull from the vector pre-filter for LLM scoring.
# Phase 5.1 — lowered from 20 to 10 to halve LLM fan-out and reduce search
# latency. The bottom half of a top-20 pre-filter was almost always either
# rejected by the LLM threshold or duplicated higher-scoring candidates, so
# the recall cost is small in normal recruiter pool sizes. Pagination via
# `offset` is currently inert (see SearchQuery docstring).
SEARCH_PREFILTER_TOP_K = 10


def _fetch_top_resume_chunks(
    session: Session,
    application_ids: list[int],
    query_vector: list[float],
    top_k: int,
) -> dict[int, str]:
    """
    Phase 5.2 — retrieve the top-K resume chunks per application by cosine
    distance to `query_vector`, plus chunk 0 always (resume header — carries
    seniority/role-level signal that may not vector-match the query).

    Concatenated in original chunk-index order so the LLM sees the resume in
    document order, not relevance order. Returns {application_id: text}.
    """
    if not application_ids:
        return {}
    rows = session.execute(
        text("""
            WITH ranked AS (
                SELECT
                    application_id,
                    chunk_index,
                    chunk_text,
                    ROW_NUMBER() OVER (
                        PARTITION BY application_id
                        ORDER BY embedding <=> (:query_vector)::vector
                    ) AS rank
                FROM resumeembedding
                WHERE application_id = ANY(:ids)
            )
            SELECT application_id, chunk_index, chunk_text
            FROM ranked
            WHERE rank <= :top_k OR chunk_index = 0
            ORDER BY application_id, chunk_index
        """),
        {
            "ids": application_ids,
            "query_vector": str(query_vector),
            "top_k": top_k,
        },
    ).fetchall()
    texts: dict[int, list[str]] = {}
    for r in rows:
        texts.setdefault(r.application_id, []).append(r.chunk_text)
    return {app_id: "\n".join(chunks) for app_id, chunks in texts.items()}


@app.post("/search/candidates", response_model=SearchResponse, tags=["AI"])
@limiter.limit("5/minute", key_func=get_user_key)
async def search_candidates(
    request: Request,
    payload: SearchQuery,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Semantic search across the authenticated recruiter's applicant pool. Protected.

    Two-stage retrieve-then-rerank (Phase 5):

    1. **Vector pre-filter** — embed the query, run pgvector cosine similarity
       restricted to applications for jobs the recruiter owns, take the top
       SEARCH_PREFILTER_TOP_K candidates above SEARCH_VECTOR_MIN_SIMILARITY.
    2. **LLM rerank** — for each pre-filter survivor, send the full resume
       text + the query to Gemini in parallel; receive an honest 0–100 score
       and a one-paragraph critique.
    3. **Final filter** — drop results below SEARCH_LLM_MIN_SCORE, sort by
       LLM score.

    Query embeddings are cached in Redis for an hour. LLM rerank scores are
    also cached, keyed by (application_id, query_text, resume_text), so
    repeated searches against unchanged data don't hit Gemini at all.

    If LLM rerank fails entirely (Gemini outage), the response falls back to
    vector-only ranking with `degraded=true` so the dashboard can surface a
    notice. Individual LLM call failures within a single request fall back
    silently to vector similarity for that candidate only.

    Pagination note (Phase 5.1): `SearchQuery.offset` is accepted for back-
    compat but currently inert. We only LLM-rerank SEARCH_PREFILTER_TOP_K
    candidates per request, so deeper paging would require re-issuing the
    pre-filter with a larger K — deferred until a recruiter actually asks.
    `has_more` is therefore always `False`.

    **Errors:**
    - `401` – missing or invalid token
    - `429` – rate limit hit (5 searches/minute per user)
    - `503` – embedding provider unavailable for pre-filter (retry shortly)
    """
    try:
        query_vector = embed_query_cached(payload.query)
    except EmbeddingError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Search temporarily unavailable: {e}",
        )

    # Cap the requested page size at the pre-filter top-K — we never have more
    # ranked candidates than that in a single request.
    effective_limit = min(payload.limit, SEARCH_PREFILTER_TOP_K)

    # --- Stage 1: vector pre-filter ---
    rows = session.execute(
        text(SEARCH_SQL),
        {
            "query_vector": str(query_vector),
            "owner_id": current_user.id,
            "min_similarity": SEARCH_VECTOR_MIN_SIMILARITY,
            "limit": SEARCH_PREFILTER_TOP_K,
            "offset": 0,
        },
    ).fetchall()

    if not rows:
        return SearchResponse(results=[], total_returned=0, has_more=False, degraded=False)

    # --- Stage 2: LLM rerank in parallel ---
    # Phase 5.2 — send top-K resume chunks (by similarity to the search query)
    # plus chunk 0, not the full resume. K is settings.RERANK_RESUME_CHUNK_TOP_K.
    application_ids = [r.application_id for r in rows]
    top_chunk_texts = _fetch_top_resume_chunks(
        session,
        application_ids,
        query_vector,
        settings.RERANK_RESUME_CHUNK_TOP_K,
    )

    pairs = [
        (r.application_id, payload.query, top_chunk_texts.get(r.application_id, r.best_match_chunk))
        for r in rows
    ]
    rerank_results = await rerank_parallel(pairs)

    # If every single rerank call failed, fall back to vector-only ordering
    degraded = all(rr is None for rr in rerank_results)

    # --- Stage 3: combine, filter, sort ---
    combined: list[SearchResult] = []
    for row, rerank in zip(rows, rerank_results):
        if rerank is not None:
            llm_score = rerank.score
            if llm_score < SEARCH_LLM_MIN_SCORE:
                continue
            combined.append(SearchResult(
                application_id=row.application_id,
                candidate_name=row.candidate_name,
                candidate_email=row.candidate_email,
                resume_url=row.resume_url,
                job_id=row.job_id,
                job_title=row.job_title,
                best_match_chunk=row.best_match_chunk,
                similarity=llm_score / 100.0,
                critique=rerank.critique,
            ))
        elif degraded:
            # Whole-request fallback path — keep candidate using vector score
            combined.append(SearchResult(
                application_id=row.application_id,
                candidate_name=row.candidate_name,
                candidate_email=row.candidate_email,
                resume_url=row.resume_url,
                job_id=row.job_id,
                job_title=row.job_title,
                best_match_chunk=row.best_match_chunk,
                similarity=float(row.similarity),
                critique=None,
            ))
        # else: individual LLM call failed but others succeeded — drop this one

    combined.sort(key=lambda r: r.similarity, reverse=True)
    page = combined[:effective_limit]

    return SearchResponse(
        results=page,
        total_returned=len(page),
        has_more=False,
        degraded=degraded,
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


@app.post("/applications/{application_id}/reanalyze", tags=["Applications"])
@limiter.limit("10/minute", key_func=get_user_key)
def reanalyze_application(
    request: Request,
    application_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Force a full re-analysis of an application regardless of error state.
    Protected.

    Unlike `/retry`, which only re-dispatches tasks that have a recorded
    error, this endpoint re-runs the full AI pipeline unconditionally:
    scoring, embedding, and cross-job matching. Useful when stored analysis
    is stale or wrong but no task has actually failed (e.g. an earlier bug
    truncated input text, the chunking strategy changed, a candidate
    re-uploaded their PDF).

    Flow:
    1. Fetch the stored PDF from MinIO and re-extract text server-side
       (the canonical extraction path — always sees the full resume,
       independent of any frontend bugs).
    2. Re-dispatch `analyze_resume_task` (scoring).
    3. Chain `embed_resume_task` → `match_jobs_task` so matching runs
       only after embedding succeeds.
    4. Clear all error columns and reset status to "pending".

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the parent job belongs to a different recruiter
    - `404` – application not found
    - `500` – the PDF could not be read from MinIO
    """
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    parent_job = session.get(JobListing, app_record.job_id)
    if not parent_job:
        raise HTTPException(status_code=404, detail="Parent job not found.")
    if parent_job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    # Re-extract resume text from MinIO. This is the canonical extraction
    # path — same one rescore_application_task uses — and always gets the
    # full resume regardless of any frontend bugs.
    s3_key = app_record.resume_url.split("/download/")[-1]
    try:
        s3 = get_s3_client()
        obj = s3.get_object(Bucket=settings.MINIO_BUCKET_NAME, Key=s3_key)
        pdf_bytes = obj["Body"].read()
        pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_content = "".join(page.extract_text() or "" for page in pdf_reader.pages)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not re-read resume from storage: {e}")

    # Invalidate cached state that becomes stale when the resume is re-embedded:
    #
    # - Chat history (Phase 4) — any prior Q&A cited specific resume chunks
    #   that will be replaced by the new embedding run. Continuing those
    #   conversations would produce broken citations and confused context,
    #   so we clear all sessions for this application.
    # - LLM rerank cache (Phase 5) — cached scores reference the prior resume
    #   text. After re-extraction the resume content may differ, so scores
    #   computed against the old text are no longer trustworthy.
    # - Cross-job matches — NOT cleared here. `match_jobs_task` is idempotent
    #   (delete-then-insert), so it self-cleans when it runs below.
    # - Query embedding cache (`emb:*`) — keyed by query text, not application,
    #   so it's not affected by changes to a single application's data.
    cleared_chats = clear_application_chats(application_id)
    cleared_rerank = clear_application_rerank_cache(application_id)

    # Re-dispatch all three pipelines. Scoring runs in parallel; embedding
    # and matching are chained because match needs embeddings to exist.
    analyze_resume_task.delay(text_content=text_content, application_id=application_id)
    chain(
        embed_resume_task.s(text_content=text_content, application_id=application_id),
        match_jobs_task.si(application_id=application_id),
    ).apply_async()

    # Reset state — clear stale errors and set status to pending while the
    # new pipeline runs. The successful tasks will clear their own errors
    # via the existing auto-recovery logic.
    app_record.scoring_error = None
    app_record.embedding_error = None
    app_record.matching_error = None
    app_record.status = "pending"
    session.add(app_record)
    session.commit()

    return {
        "message": "Full re-analysis dispatched",
        "application_id": application_id,
        "chat_sessions_cleared": cleared_chats,
        "rerank_cache_entries_cleared": cleared_rerank,
    }


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
            critique=match.critique,
        )
        for match, job in rows
    ]


@app.get(
    "/jobs/{job_id}/cross-applicants",
    response_model=List[CrossApplicantResult],
    tags=["AI"],
)
@limiter.limit("60/minute", key_func=get_user_key)
def get_job_cross_applicants(
    request: Request,
    job_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    List candidates who applied to a *different* job owned by the same recruiter
    but are strong fits for this one (Phase 3.1 — the inverse view of cross-job
    matches). Read-only DB query — no Gemini call.

    Cross-job matches are only computed within a recruiter's own pool
    (`cand.owner_id = orig.owner_id` clause in CROSS_JOB_MATCH_SQL), so verifying
    ownership of the target `job_id` is sufficient — the originating jobs are
    necessarily owned by the same recruiter.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the job belongs to a different recruiter
    - `404` – job not found
    """
    job = session.get(JobListing, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    rows = session.execute(
        text("""
            SELECT
                a.id            AS application_id,
                a.candidate_name,
                a.candidate_email,
                a.resume_url,
                orig.id         AS original_job_id,
                orig.title      AS original_job_title,
                m.similarity,
                m.critique
            FROM crossjobmatch m
            JOIN application a   ON a.id = m.application_id
            JOIN joblisting orig ON orig.id = a.job_id
            WHERE m.matched_job_id = :job_id
            ORDER BY m.similarity DESC
            LIMIT 20
        """),
        {"job_id": job_id},
    ).fetchall()

    return [
        CrossApplicantResult(
            application_id=r.application_id,
            candidate_name=r.candidate_name,
            candidate_email=r.candidate_email,
            resume_url=r.resume_url,
            original_job_id=r.original_job_id,
            original_job_title=r.original_job_title,
            similarity=float(r.similarity),
            critique=r.critique,
        )
        for r in rows
    ]


@app.post("/applications/{application_id}/match-refresh", tags=["AI"])
@limiter.limit("5/minute", key_func=get_user_key)   # Phase 5 — each match now fires ~10 LLM calls
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
@limiter.limit("1/hour", key_func=get_user_key)     # Phase 5 — bulk × LLM rerank is heavy
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


# ---------------------------------------------------------------------------
# Phase 4 — RAG Q&A streaming chat endpoint
# ---------------------------------------------------------------------------
#
# SQL for per-application chunk retrieval. Much simpler than the Phase 2 search
# SQL because we're scoped to one resume and want every relevant chunk back,
# not "one row per candidate".
CHAT_RETRIEVAL_SQL = """
SELECT
    chunk_index,
    chunk_text,
    1 - (embedding <=> CAST(:query_vector AS vector)) AS similarity
FROM resumeembedding
WHERE application_id = :application_id
ORDER BY embedding <=> CAST(:query_vector AS vector)
LIMIT :top_k
"""

CHAT_TOP_K = 5


def _chat_error_event(exc: BaseException, *, default_message: str) -> str:
    """
    Convert any LLM-or-embedding-side exception raised during a chat stream
    into a clean SSE event. Daily-quota and transient-unavailable cases use
    the soft `system_message` channel (amber notice in the dashboard); only
    truly unexpected failures go through `error` (red). We deliberately never
    surface the raw provider exception text — Gemini's 429 body is a 2 KB JSON
    blob that's useless to the recruiter.
    """
    # Unwrap a single layer of `raise X from e` so EmbeddingError("Failed...: <raw>")
    # is classified by the original Gemini exception, not the wrapper.
    candidate = exc.__cause__ if exc.__cause__ is not None else exc
    classified = _classify_gemini_error(candidate) or _classify_gemini_error(exc)

    if isinstance(classified, GeminiQuotaExhaustedError):
        return _sse("system_message", {
            "content": (
                "Gemini's daily free-tier quota has been exhausted. "
                "The chat will be available again once the quota resets "
                "(usually within 24 hours), or you can upgrade your Gemini API plan for higher limits."
            ),
        })
    if isinstance(classified, GeminiUnavailableError):
        return _sse("system_message", {
            "content": (
                "Gemini is experiencing high demand right now and could not respond. "
                "Please try again in a few minutes."
            ),
        })
    # Unknown failure — keep the message generic; the raw exception is logged
    # server-side but not shown to the user.
    print(f"chat: unexpected error: {type(exc).__name__}: {exc}")
    return _sse("error", {"detail": default_message})


def _sse(event_type: str, payload: dict) -> str:
    """Format a JSON payload as a server-sent event."""
    body = json.dumps({"type": event_type, **payload})
    return f"data: {body}\n\n"


@app.post("/applications/{application_id}/chat", tags=["AI"])
@limiter.limit("5/minute", key_func=get_user_key)
def chat_with_resume(
    request: Request,
    application_id: int,
    payload: ChatRequest,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Streaming RAG Q&A over a single candidate's resume. Protected.

    Embeds the recruiter's question, retrieves the top-5 most relevant chunks
    of *this candidate's* resume, and streams Gemini's grounded answer back
    via server-sent events. The recruiter's frontend renders tokens
    incrementally and shows the cited chunks below the answer.

    See `docs/ai-features/phase-4-rag-qa.md` for the SSE event schema, the
    citation-required system prompt, and the conversation-history protocol
    (frontend-managed sliding window of 6 turns).

    **Errors:**
    - `401` – missing or invalid token
    - `403` – the parent job belongs to a different recruiter
    - `404` – application not found
    - `429` – rate limit hit (5 chat requests / minute per user)
    - SSE `error` event – Gemini failures mid-stream are recoverable; surfaced
      to the frontend as a recruiter-readable error inside the open stream
    """
    # --- Authorization (runs BEFORE the stream opens) ---
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    parent_job = session.get(JobListing, app_record.job_id)
    if not parent_job or parent_job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    question = payload.question
    session_id = payload.session_id
    user_id = current_user.id

    def event_stream():
        # --- Early exit: this resume has no embeddings yet ---
        chunks_exist = session.execute(
            select(ResumeEmbedding.id)
            .where(ResumeEmbedding.application_id == application_id)
            .limit(1)
        ).first()
        if not chunks_exist:
            yield _sse("system_message", {
                "content": (
                    "This candidate's resume has not been processed for AI Q&A yet. "
                    "Try the Retry button on the candidate row, or upload again."
                )
            })
            yield _sse("done", {})
            return

        # --- Load prior conversation from Redis ---
        # Falls back to empty list on Redis error — chat still works without context
        history = load_history(user_id, application_id, session_id)

        # --- Embed the question (Phase 2 Redis cache reused) ---
        try:
            query_vector = embed_query_cached(question)
        except EmbeddingError as e:
            yield _chat_error_event(e, default_message="Could not process your question right now. Please try again.")
            yield _sse("done", {})
            return

        # --- Retrieve top-K chunks from this resume only ---
        rows = session.execute(
            text(CHAT_RETRIEVAL_SQL),
            {
                "query_vector": str(query_vector),
                "application_id": application_id,
                "top_k": CHAT_TOP_K,
            },
        ).fetchall()

        citations = [
            Citation(
                chunk_index=row.chunk_index,
                chunk_text=row.chunk_text,
                similarity=float(row.similarity),
            )
            for row in rows
        ]

        # --- Stream the grounded answer from Gemini ---
        accumulated = ""
        try:
            for token in stream_rag_answer(question, citations, history):
                accumulated += token
                yield _sse("token", {"content": token})
        except Exception as e:
            yield _chat_error_event(e, default_message="Sorry, the AI service could not generate a response. Please try again.")
            yield _sse("done", {})
            return

        # --- After all tokens, send citations + done ---
        yield _sse(
            "citations",
            {"citations": [c.model_dump() for c in citations]},
        )
        yield _sse("done", {})

        # --- Persist this turn to Redis AFTER the response is sent.
        # We persist both the user's question and the full assistant reply so
        # the next request can see them in history. If accumulated is empty
        # (unlikely — would mean Gemini sent zero tokens), we skip storage.
        if accumulated:
            append_turn(user_id, application_id, session_id, "user", question)
            append_turn(user_id, application_id, session_id, "assistant", accumulated)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Nginx: do not buffer SSE
        },
    )


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


# ---------------------------------------------------------------------------
# Phase 6 — Outreach drafts (list / send / discard)
# ---------------------------------------------------------------------------
#
# The endpoints below own the lifecycle of AI-drafted outreach emails. The
# *creation* of drafts happens elsewhere — either via the chat agent's
# `draft_email` tool (Phase 6 main agent) or the one-click cross-match-invite
# endpoint. These endpoints handle viewing, sending, and discarding the
# resulting drafts. Sending is the ONLY way an AI-drafted email ever leaves
# the system; the agent itself can never trigger a network send.

def _draft_to_public(session: Session, draft: OutreachEmail) -> EmailDraftPublic:
    """Hydrate an OutreachEmail row with candidate + target-job info for the UI."""
    app_record = session.get(Application, draft.application_id)
    target_job = session.get(JobListing, draft.target_job_id) if draft.target_job_id else None
    return EmailDraftPublic(
        id=draft.id,
        application_id=draft.application_id,
        candidate_name=app_record.candidate_name if app_record else "(application deleted)",
        candidate_email=app_record.candidate_email if app_record else "",
        intent=draft.intent,
        target_job_id=draft.target_job_id,
        target_job_title=target_job.title if target_job else None,
        subject=draft.subject,
        body=draft.body,
        status=draft.status,
        created_at=draft.created_at,
        sent_at=draft.sent_at,
    )


@app.get("/assistant/drafts", response_model=List[EmailDraftPublic], tags=["Assistant"])
@limiter.limit("60/minute", key_func=get_user_key)
def list_outreach_drafts(
    request: Request,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
    application_id: int | None = None,
    status: str | None = None,
):
    """
    List outreach drafts owned by the authenticated recruiter. Protected.

    Optional filters:
      - `?application_id=N` — only drafts for that candidate
      - `?status=draft|sent|discarded` — defaults to all

    Multi-tenancy is enforced via `recruiter_id == current_user.id`.

    **Errors:**
    - `401` – missing or invalid token
    - `429` – rate limit hit (60/min per user)
    """
    statement = select(OutreachEmail).where(OutreachEmail.recruiter_id == current_user.id)
    if application_id is not None:
        statement = statement.where(OutreachEmail.application_id == application_id)
    if status is not None:
        statement = statement.where(OutreachEmail.status == status)
    statement = statement.order_by(OutreachEmail.created_at.desc())

    drafts = session.exec(statement).all()
    return [_draft_to_public(session, d) for d in drafts]


@app.post("/assistant/drafts/{draft_id}/send", response_model=DraftSendResponse, tags=["Assistant"])
@limiter.limit("30/hour", key_func=get_user_key)
def send_outreach_draft(
    request: Request,
    draft_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Send a previously-created outreach draft via Resend. Protected.

    Updates the row's `status` to `"sent"` and stamps `sent_at`. Idempotent:
    a second send attempt on the same draft returns 409.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – draft belongs to a different recruiter
    - `404` – draft not found
    - `409` – draft is not in `status="draft"` (already sent or discarded)
    - `429` – rate limit hit (30/hour per user)
    - `502` – Resend rejected the send; status stays `"draft"` so the recruiter can retry
    """
    # Local import keeps the auth/route module decoupled from the email side
    # in tests; only this endpoint actually needs Resend at runtime.
    from app.email import send_outreach_email
    from resend.exceptions import ResendError

    draft = session.get(OutreachEmail, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    if draft.recruiter_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")
    if draft.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Draft is in status '{draft.status}' and cannot be sent again.",
        )

    app_record = session.get(Application, draft.application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application no longer exists.")

    try:
        message_id = send_outreach_email(
            to_email=app_record.candidate_email,
            subject=draft.subject,
            body=draft.body,
            recruiter_name=current_user.full_name or current_user.email,
            recruiter_email=current_user.email,
        )
    except ResendError as e:
        # Don't flip status — recruiter can retry later
        raise HTTPException(status_code=502, detail=f"Email provider rejected the send: {e}")

    draft.status = "sent"
    draft.sent_at = datetime.now()
    session.add(draft)
    session.commit()
    session.refresh(draft)

    return DraftSendResponse(status="sent", sent_at=draft.sent_at, message_id=message_id)


@app.post("/assistant/drafts/{draft_id}/discard", tags=["Assistant"])
@limiter.limit("30/minute", key_func=get_user_key)
def discard_outreach_draft(
    request: Request,
    draft_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Soft-delete an outreach draft. Marks `status="discarded"` rather than
    deleting the row so we keep the audit trail of what the LLM proposed.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – draft belongs to a different recruiter
    - `404` – draft not found
    - `409` – draft is not in `status="draft"` (already sent — cannot un-send)
    - `429` – rate limit hit (30/min per user)
    """
    draft = session.get(OutreachEmail, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    if draft.recruiter_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")
    if draft.status == "sent":
        raise HTTPException(
            status_code=409,
            detail="Draft has already been sent and cannot be discarded.",
        )
    if draft.status == "discarded":
        return {"status": "discarded", "message": "Already discarded."}

    draft.status = "discarded"
    session.add(draft)
    session.commit()
    return {"status": "discarded"}


@app.post(
    "/applications/{application_id}/cross-match-invite",
    response_model=EmailDraftPublic,
    tags=["Assistant"],
)
@limiter.limit("30/hour", key_func=get_user_key)
def cross_match_invite(
    request: Request,
    application_id: int,
    payload: CrossMatchInviteRequest,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    One-click contextual draft: invite a candidate (who applied to one role)
    to apply to a *different* role the same recruiter has posted. Calls the
    `draft_email_for_application` helper directly — no agent loop — and
    persists the result in `outreach_email`. The candidate-modal UI clicks
    this when the recruiter hits the "Draft invite email" button on a
    cross-match row. Recruiter then reviews + clicks Send (separate endpoint).

    Multi-tenancy: both the candidate's application's parent job *and* the
    target matched job must belong to `current_user`. Cross-job matches are
    only computed within a recruiter's pool, but we double-check defensively.

    **Errors:**
    - `401` – missing or invalid token
    - `403` – application or matched job belongs to a different recruiter
    - `404` – application or matched job not found
    - `422` – draft generation produced unusable output (rare)
    - `429` – rate limit hit (30/hour per user)
    - `503` – Gemini quota exhausted or temporarily unavailable
    """
    from app.outreach import DraftEmailError, draft_email_for_application

    # --- ownership checks ---
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    parent_job = session.get(JobListing, app_record.job_id)
    if not parent_job or parent_job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    target_job = session.get(JobListing, payload.matched_job_id)
    if not target_job:
        raise HTTPException(status_code=404, detail="Matched job not found.")
    if target_job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    if target_job.id == parent_job.id:
        raise HTTPException(
            status_code=400,
            detail="The matched job and the applied job are the same — not a cross-match.",
        )

    # --- draft via the shared LLM helper ---
    try:
        draft = draft_email_for_application(
            session,
            application_id=application_id,
            target_job_id=payload.matched_job_id,
            intent="cross_match_invite",
            recruiter=current_user,
            tone="warm and inviting",
        )
    except GeminiQuotaExhaustedError:
        raise HTTPException(
            status_code=503,
            detail="Gemini's daily free-tier quota has been exhausted. "
                   "Try again after the quota resets (~24 hours) or upgrade your plan.",
        )
    except GeminiUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Gemini is experiencing high demand. Please try again in a few minutes.",
        )
    except DraftEmailError as e:
        raise HTTPException(status_code=422, detail=f"Draft generation failed: {e}")

    return _draft_to_public(session, draft)


# ---------------------------------------------------------------------------
# Phase 6 — Chat assistant turn (SSE) and reset
# ---------------------------------------------------------------------------

class _AssistantTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.post("/assistant/turn", tags=["Assistant"])
@limiter.limit("10/minute", key_func=get_user_key)
async def assistant_turn(
    request: Request,
    payload: _AssistantTurnRequest,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Drive one turn of the recruiter assistant agent. SSE stream.

    Each call:
      1. Inside the streaming generator: opens a fresh DB session and sets
         the per-request agent context (user + session) via contextvar so
         tools can run with auth scope. Setting it here (not in the
         endpoint body) guarantees the contextvar is alive in the same
         coroutine that LangGraph uses to execute the tools.
      2. Loads the rolling conversation history for this recruiter from Redis.
      3. Runs the LangGraph agent loop, streaming intermediate events as SSE.
      4. Persists the new conversation turns back to Redis on completion.

    SSE event types emitted:
      thinking       — soft status text
      tool_call      — agent invoked a tool {tool_call_id, name, args}
      tool_result    — tool completed {tool_call_id, name, summary, errored}
      email_draft    — draft_email tool produced a draft (full draft fields)
      token          — token of final synthesis
      system_message — soft amber notice (quota exhausted, transient unavail)
      error          — hard red notice (unexpected failure)
      done           — end of turn

    Rate-limited to 10/min per user because each turn fires multiple LLM calls.
    """
    from app.agent import run_turn_stream, set_agent_context
    from app.database import engine as _engine

    user_id = current_user.id
    user_email = current_user.email
    user_message = payload.message

    async def event_stream():
        # Open a dedicated DB session for the turn so the agent context can
        # safely outlive the request-scoped session injected by FastAPI.
        # Set the contextvar inside the generator so it's bound to the same
        # asyncio task LangGraph uses to drive tool execution.
        from app.models import User as _User
        with Session(_engine) as agent_session:
            recruiter = agent_session.get(_User, user_id)
            if recruiter is None:
                yield f"event: error\ndata: {{\"detail\": \"User not found\"}}\n\n"
                yield "event: done\ndata: {}\n\n"
                return
            set_agent_context(recruiter, agent_session)
            print(f"agent: turn for user_id={user_id} email={user_email} message={user_message[:120]!r}")
            async for sse_chunk in run_turn_stream(user_id, user_message):
                yield sse_chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/assistant/reset", tags=["Assistant"])
@limiter.limit("30/minute", key_func=get_user_key)
def assistant_reset(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Clear the recruiter's rolling chat history in Redis. New conversations
    start fresh after this call. Drafts in `outreach_email` are NOT affected.
    """
    from app.agent import clear_history
    clear_history(current_user.id)
    return {"status": "reset"}
