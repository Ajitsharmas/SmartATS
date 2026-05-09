# ----------------------------------------------------------------------------------------------------
# Purpose: The entry point of  ATS endpoint
# Author: Ajit Sharma S
# ----------------------------------------------------------------------------------------------------
from fastapi import FastAPI, Depends, HTTPException
from sqlmodel import Session, select
from typing import Annotated, List
from contextlib import asynccontextmanager

# Import our local modules
from app.database import create_db_and_tables, get_session
from app.models import JobListing, AnalysisRequest
from app.ai import get_ai_provider, AIProvider


# 1. Lifespan Context Manager
# This runs code before the app starts and after it shuts down.
# We use it to create our database tables automatically on startup.
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Startup: Creating database tables...")
    create_db_and_tables()
    yield
    print("Shutdown: Cleaning up resources...")


app = FastAPI(
    title = "ATS",
    description = "An AI powered Applicant Tracking System (ATS)",
    version = "1.0.0",
    lifespan=lifespan
)


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
@app.post("/jobs", response_model=JobListing)
def create_job(job: JobListing, session: SessionDep):
    """
    Create a new Job Listing.
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


