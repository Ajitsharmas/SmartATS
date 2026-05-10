# ----------------------------------------------------------------------------------------------------
# Purpose: The entry point of  ATS endpoint
# Author: Ajit Sharma S
# ----------------------------------------------------------------------------------------------------
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from sqlmodel import Session, select
from typing import Annotated, List
from contextlib import asynccontextmanager
import uuid
import pypdf
import io 

# Import our local modules
from app.database import create_db_and_tables, get_session
from app.models import JobListing, AnalysisRequest, User
from app.ai import get_ai_provider, AIProvider
from app.utils import init_storage, get_s3_client
# Import the tasd to trigger it, and the app instance to check results.
from app.worker import analyze_resume_task, celery_app
from celery.result import AsyncResult
from app.config import settings
# Security related imports
from app.auth import auth_router, get_current_user # Import the router and dependency
# 1. Lifespan Context Manager
# This runs code before the app starts and after it shuts down.
# We use it to create our database tables automatically on startup.
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Startup: Creating database tables...")
    create_db_and_tables()
    print("Startup: Checking Object Storage...")
    init_storage()
    yield
    print("Shutdown: Cleaning up resources...")

app = FastAPI(
    title = "ATS",
    description = "An AI powered Applicant Tracking System (ATS)",
    version = "1.0.0",
    lifespan=lifespan
)

# 2. Register the Auth Router
# This adds /token and /register to our API
app.include_router(auth_router)

# 2 Define a Dependency Injection type alias
# This makes our path operations cleaner.
SessionDep = Annotated[Session, Depends(get_session)]

#AI Dependency
# This injects the correct AI class based on our settings(Gemini or LLama)
AIDep = Annotated[AIProvider, Depends(get_ai_provider)]


@app.get("/health")
async def health_check():
    """
    A simple heartbeat endpoint to verify the service is up.
    """
    return {"status": "ok", "message": "ATS is ready to serve!"}

# ----------------------------JOB ROUTES------------------------------------------

# 3. Create a Job (POST)
# If user is not logged in, this fuction will NEVER run.
@app.post("/jobs", response_model=JobListing)
def create_job(job: JobListing,
               session: SessionDep,
               current_user: Annotated[User, Depends(get_current_user)]):
    """
    Create a new Job Listing (Registered Recruiters only)
    """

    session.add(job)        # Add to Session, ready to Save to DB
    session.commit()        # Save to DB
    session.refresh(job)    # get new ID from DB
    return job


# 4. List all Jobs (GET)
@app.get("/jobs", response_model=List[JobListing])
def list_jobs(session: SessionDep):
    """
    Retrieve all open job positions
    """

    # Write the query: "SELECT * FROM joblisting"
    statement = select(JobListing)
    jobs = session.exec(statement).all()
    return jobs


# 5. ------------------------------------------------AI Analysis Routes-------------------------------------------------------------
@app.post("/analyze")
async def analyze_resume_text(request: AnalysisRequest, ai: AIDep):
    """
    Sends raw text to the configured AI provider.
    Enforces a strict JSON output ormat (Score + Critique) to match the PRD Requirements.
    """

    # We craft a specific prompt to forcre the AI into an engineering mindset.
    # We explicitly ask for JSON so we can parse it programatically later.

    prompt = f"""
    You are an expert tech recruiter. Analyze the following resume text against a generic Senior Developer role.

    Return your response in this exact JSON format:
    {{
        "score": (interger 0-100),
        "critique": (string, concise summary of gasps and strengths)
    }}

    Resume Text:
    {request.text}
    """

    # We await. the result. The Event loop is free to handle other requests while waiting.
    analysis = await ai.analyze_text(prompt)

    return {"analysis": analysis}


# -------------------------------File Upload Router---------------------------------------------
@app.post("/upload")
async def upload_resume(
    current_user: Annotated[User, Depends(get_current_user)], # Required Arguments need to come before Default Arguments always
    file: UploadFile = File(...)
):
    """
    Accepts a temporary file, validates it, extracts text, and saves to minIO
    """
    # 1. Validation: Check File Extension 
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type. Only PDFs are allowed.")
    
    # 2. Validation: Magic Numbners (The Real Security Check)
    # Read the first 4 bytes to verify it's a true PDF header (%PDF)
    header = await file.read(4)
    if header != b"%PDF":
        raise HTTPException(status_code=400, detail="Corrupt or invalid PDF file.")

    # CRITICAL: Reset the cursor
    # We just read 4 bytes. If we don't rewind, the PDF reader will start reading from byte 5 and fail.
    await file.seek(0)

    # 3. Read the content for processing
    # In a perfect world, we should stream this. But pypdf requires file in memory.
    # Since resumes are small (< 5MB), reading to RAM is acceptable here.
    content = await file.read()

    # 4. Text Extraction (The Intelligence)
    try:
        # We wrap the raw bytes in BytesIO so pypdf thinks it's a real file
        pdf_reader = pypdf.PdfReader(io.BytesIO(content))
        extracted_text=""
        for page in pdf_reader.pages:
            extracted_text += page.extract_text() or ""
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract teext: {str(e)}")

    
    # 5. Generate uniqure Key (The Storage Strategy)
    file_id = str(uuid.uuid4())
    s3_key = f"{file_id}.pdf"

    # 6. Upload to MinIO (The Vault)
    try:
        s3 = get_s3_client()
        s3.put_object(
            Bucket=settings.MINIO_BUCKET_NAME,
            Key=s3_key,
            Body=content,
            ContentType="application/pdf"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage Error: {str(e)}")
    
    # 7. Return Metadata
    return {
        "file_id": file_id,
        "filename": file.filename,
        "s3_key": s3_key,
        "extracted_text_preview": extracted_text[:200] + "..." # Peek at thje result
    }


#------------------------------------------Async AI Routes (Fire and forget Pattern)--------------------------------------------------------
@app.post("/analyze_async")
async def analyze_resume_async(
    request: AnalysisRequest,
    current_user: Annotated[User, Depends(get_current_user)]
):
    """
    Triggers a background AI analysis.
    Returns a Task ID instantly. Does NOT wait for the AI to finish. 
    """

    # 1. Push the Task to Redis
    # .delay() is the magic method. It serializes the function call
    # and sends it to the Message Brojker.
    task = analyze_resume_task.delay(request.text)

    # 2. Return the Ticket (Task ID) immediately
    # We use status_code=202 (Acceepted) to indicate processing has started but not finished.
    return {
        "message": "Resume accepted for processing. ",
        "task_id": task.id,
        "status_url": f"/analyze_async/{task.id}"
    }

# Check status of resume processing task
@app.get("/analyze_async/{task_id}")
async def get_analyze_resume_async_status(task_id: str):
    """
    Polls the status of a background task.
    Clients should call this every few seconds until status is 'Done'.
    """

    # 1. Fetch the Task Metadata from Redis
    # AsyncResult looks up the task_id in the Result Backend.
    task_result = AsyncResult(task_id, app=celery_app)

    # 2. Check the Lifecycle State
    if task_result.state == "PENDING":
        return {
            "status": "Processing",
            "result": None
        }
    elif task_result.state == "SUCCESS":
        # The worker finished successfully. The restuirn value is in .result
        return {
            "status": "Done",
            "result": task_result.result
        }
    elif task_result.state == "FAILURE":
        # The worker crashed (e.g. AI API Error). The exception is in .result
        return {
            "status": "Failed",
            "error": str(task_result.result)
        }
    
    # Catch-all for other states like "RETRY" and "STARTED"
    return {"status": task_result.state}
        