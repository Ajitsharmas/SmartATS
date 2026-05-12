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
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.ai import AIProvider, get_ai_provider
from app.auth import auth_router, get_current_user
from app.config import settings

# Import our local modules
from app.database import create_db_and_tables, get_session

# CRITICAL: We import ApplicationSubmit here to handle the full frontend payload
from app.models import AnalysisRequest, Application, ApplicationSubmit, JobListing, JobListingUpdate, User
from app.utils import get_s3_client, init_storage
from sqlalchemy.exc import IntegrityError

from app.worker import analyze_resume_task, celery_app, rescore_application_task


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

# 2. MOUNT STATIC FILES
# This tells FastAPI: "If a user asks for /static/css/style.css, look in the app/static folder."
# This effectively turns our API into a Web Server for the frontend files.
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# 3. PAGE ROUTES (Serving HTML)
# These endpoints just return the raw HTML files.
# The JavaScript inside those files will call our JSON APIs later.
_NO_CACHE = {"Cache-Control": "no-store"}

@app.get("/")
async def read_root():
    return FileResponse("app/static/index.html", headers=_NO_CACHE)


@app.get("/dashboard")
async def read_dashboard():
    return FileResponse("app/static/dashboard.html", headers=_NO_CACHE)


@app.get("/login")
async def read_login():
    return FileResponse("app/static/login.html", headers=_NO_CACHE)


@app.get("/register")
async def read_register():
    return FileResponse("app/static/register.html", headers=_NO_CACHE)


@app.get("/verify-email")
async def read_verify_email():
    return FileResponse("app/static/verify-email.html", headers=_NO_CACHE)


@app.get("/reset-password")
async def read_reset_password():
    return FileResponse("app/static/reset-password.html", headers=_NO_CACHE)


# --- 4. REGISTER MODULES ---
# We attach the Auth routes (/token, /register) defined in auth.py
app.include_router(auth_router)


# 5. DEPENDENCY INJECTION CONFIGURATION
# This makes our path operations cleaner and easier to test.
SessionDep = Annotated[Session, Depends(get_session)]

# AI Dependency: Injects the correct AI class based on settings (Gemini/Llama)
AIDep = Annotated[AIProvider, Depends(get_ai_provider)]


@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "SmartATS is ready to serve 🚀"}


# --- JOB ROUTES ---


# Create a Job (POST)
# SECURITY: Notice 'current_user' dependency.
# If the user does not have a valid Token, FastAPI rejects this request (401).
@app.post("/jobs", response_model=JobListing)
def create_job(
    job: JobListing,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Create a new Job Listing (Protected: Recruiters Only).
    """
    job.owner_id = current_user.id
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# List all Jobs (GET) — PUBLIC for candidates
@app.get("/jobs", response_model=List[JobListing])
def list_jobs(session: SessionDep):
    """
    Retrieve all open job positions (public, used by candidates).
    """
    return session.exec(select(JobListing)).all()


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(
    job_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    job = session.get(JobListing, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this job.")
    session.delete(job)   # CASCADE removes all applications automatically
    session.commit()


@app.patch("/jobs/{job_id}", response_model=JobListing)
def update_job(
    job_id: int,
    updates: JobListingUpdate,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
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

    # Re-score only when fields that affect candidate fit actually change.
    # Salary is a business constraint invisible to the AI — no re-score needed.
    RESCORE_FIELDS = {"title", "description", "skills", "location"}
    if changed_fields & RESCORE_FIELDS:
        applications = session.exec(select(Application).where(Application.job_id == job_id)).all()
        for app_record in applications:
            app_record.status = "pending"
            app_record.ai_score = 0
            app_record.ai_critique = None
            session.add(app_record)
        session.commit()
        for app_record in applications:
            rescore_application_task.delay(app_record.id)

    return job


# List only the logged-in recruiter's jobs — PROTECTED
@app.get("/my-jobs", response_model=List[JobListing])
def list_my_jobs(
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Retrieve job listings owned by the current recruiter.
    """
    return session.exec(
        select(JobListing).where(JobListing.owner_id == current_user.id)
    ).all()


# --- AI ANALYSIS ROUTES (Direct Test) ---
@app.post("/analyze")
async def analyze_resume_text(request: AnalysisRequest, ai: AIDep):
    """
    Direct endpoint to test AI output without saving to DB.
    Useful for debugging the LLM prompt.
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
@app.post("/upload")
async def upload_resume(
    # REMOVED: current_user dependency.
    # Candidates are anonymous users. We must allow them to upload without login.
    file: UploadFile = File(...),
):
    """
    Accepts a PDF file, validates it, extracts text, and saves to MinIO.
    PUBLIC ENDPOINT (No Auth Required)
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


@app.get("/download/{s3_key}")
async def download_resume(s3_key: str):
    """
    Proxy endpoint: fetches the PDF from MinIO (internal) and streams it to the browser.
    MinIO is not publicly accessible, so the browser must go through FastAPI.
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
@app.get("/applications/{job_id}", response_model=List[Application])
def get_applications(
    job_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Recruiter Dashboard: View all applicants for a specific job.
    Returns the list sorted by 'ai_score' so the best candidates appear first.
    """
    statement = (
        select(Application)
        .where(Application.job_id == job_id)
        .order_by(Application.ai_score.desc())
    )
    results = session.exec(statement).all()
    return results


# --- Processing ROUTES (The Main Workflow) ---
@app.post("/process")
async def process_resume(payload: ApplicationSubmit, session: SessionDep):
    """
    The Orchestrator Endpoint.
    1. Receives the full application package (Job ID + Candidate + Resume Text).
    2. SAVES the application to Postgres immediately (Status: Pending).
    3. DISPATCHES the AI task to the Celery Worker.
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

    # B. Trigger Worker
    # CRITICAL: We pass the 'application_id' (app_record.id) to the worker.
    # This allows the worker to "call us back" (update the DB) when it finishes.
    task = analyze_resume_task.delay(
        text_content=payload.request.text, application_id=app_record.id
    )

    return {
        "message": "Application received",
        "task_id": task.id,
        "application_id": app_record.id,
    }


@app.get("/process/{task_id}")
async def get_processing_status(task_id: str):
    """
    Polls the status of a background task.
    Used by the frontend to show progress bars.
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
