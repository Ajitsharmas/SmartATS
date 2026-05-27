# ---------------------------------------------------------------------------
# Purpose: Pydantic Models and SQLModel Tables
# ---------------------------------------------------------------------------

from datetime import datetime
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    field_validator,
)  # Added EmailStr for robust validation
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, ForeignKey, Text
from sqlmodel import Field, SQLModel, UniqueConstraint

from app.config import settings


# --- JOB LISTING MODELS ---
class JobListing(SQLModel, table=True):
    """
    Represents a Job Post visible to candidates.
    """

    # 1. THE CONFIGURATION
    # Force validation on assignment to prevent "Lazy" data handling.
    # If we try to set job.salary_range = "-500" in code, this will crash immediately.
    model_config = ConfigDict(validate_assignment=True)

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int | None = Field(default=None, foreign_key="user.id", index=True)

    # 2. THE LENGTH CONSTRAINTS
    # Title: Must be professional (no 3-letter acronyms like "Dev").
    title: str = Field(min_length=5, max_length=100)

    # Description: Must be substantial. You can't hire with 1 word.
    description: str = Field(min_length=10)

    skills: str
    location: str
    salary_range: str | None = None

    created_at: datetime = Field(default_factory=datetime.now)

    # Set by embed_job_task.on_failure when the job-description embedding
    # pipeline exhausts its retries. Non-null = the job currently has no
    # usable embedding for cross-job matching.
    embedding_error: str | None = None

    # 3. THE CUSTOM LOGIC
    # Pydantic Validator to enforce business rules that types can't catch.
    @field_validator("salary_range", mode="after")
    @classmethod
    def validate_salary(cls, value: str | None) -> str | None:
        """
        Enforce PRD Rule: No negative numbers in salary.
        """
        if value is None:
            return None

        if value.strip().startswith("-"):
            raise ValueError("Salary cannot be negative. We pay people here!")

        return value


# --- USER MODELS (The 3-Layer Pattern) ---
# We split the User into Base, Table, Input, and Output to separate concerns.


class UserBase(SQLModel):
    """
    Layer 1: The Foundation.
    Shared properties used for both reading and writing.
    EmailStr ensures the string is actually a valid email format (x@y.z).
    """

    email: EmailStr = Field(unique=True, index=True)
    full_name: str | None = None
    is_active: bool = True


class User(UserBase, table=True):
    """
    Layer 2: The Database Table.
    This acts as the Vault. It contains the sensitive 'hashed_password'.
    We never return this model directly to the frontend.
    """

    id: int | None = Field(default=None, primary_key=True)
    hashed_password: str
    created_at: datetime = Field(default_factory=datetime.now)

    # Email verification
    is_verified: bool = Field(default=False)
    verification_token: str | None = Field(default=None)

    # Password reset
    reset_token: str | None = Field(default=None)


class UserCreate(UserBase):
    """
    Layer 3a: The Input (Registration).
    Matches the JSON payload sent by the Registration Form.
    Contains 'password' (plain text) which we will hash before saving.
    """

    password: str


class UserPublic(UserBase):
    """
    Layer 3b: The Output (Response).
    This is what we send back to the browser.
    CRITICAL: It excludes 'password' and 'hashed_password' by omitting them.
    """

    id: int
    created_at: datetime


# --- AI & APPLICATION MODELS ---


class AnalysisRequest(SQLModel):
    """
    Simple schema for testing the AI endpoint directly with raw text.
    """

    text: str


# --- NEW: THE APPLICATION PACKAGE (BFF Pattern) ---
# This is a "Composite Model". It combines data from the UI (Name, Email)
# with data from the Analysis (Resume Text) into a single payload.
# This matches exactly what api.js sends in the JSON body.
class ApplicationSubmit(SQLModel):
    request: AnalysisRequest  # Nested JSON: { "text": "..." }
    job_id: int
    candidate_name: str
    candidate_email: str
    resume_url: str


class Application(SQLModel, table=True):
    """
    The "Join Table" connecting a Candidate to a Job Listing.
    Stores the status of the application and the permanent AI Score.
    """

    __table_args__ = (
        UniqueConstraint("job_id", "candidate_email", name="uq_application_job_email"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # CASCADE ensures all applications are deleted when the parent job is deleted
    job_id: int = Field(
        sa_column=Column(Integer, ForeignKey("joblisting.id", ondelete="CASCADE"), nullable=False)
    )

    # Candidate Info (Snapshot at time of application)
    candidate_email: str
    candidate_name: str
    resume_url: str

    # AI Results (Populated by the Celery Worker)
    ai_score: int = 0
    ai_critique: Optional[str] = None
    status: str = "pending"  # pending -> processed | pending -> failed -> pending (after retry)

    # Failure tracking (populated by Celery task on_failure hooks when retries exhaust).
    # When any of these is non-null, status becomes "failed" and the dashboard
    # offers a Retry button that re-dispatches whichever task(s) failed.
    scoring_error: Optional[str] = None
    embedding_error: Optional[str] = None
    matching_error: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.now)


class JobListingUpdate(SQLModel):
    """Partial-update schema for PATCH /jobs/{id}."""
    title: str | None = Field(default=None, min_length=5, max_length=100)
    description: str | None = Field(default=None, min_length=10)
    skills: str | None = None
    location: str | None = None
    salary_range: str | None = None

    @field_validator("salary_range", mode="after")
    @classmethod
    def validate_salary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.strip().startswith("-"):
            raise ValueError("Salary cannot be negative.")
        return value


# --- EMBEDDING MODELS (Phase 0) ---
#
# These tables store vector embeddings of resume chunks and job description
# chunks for use in semantic search, RAG Q&A, and cross-job matching.
# The HNSW indexes on the `embedding` column are created at startup in
# `app/database.py` because SQLModel cannot express them declaratively.


class ResumeEmbedding(SQLModel, table=True):
    """
    A single embedded chunk of a candidate's resume.
    Multiple rows per application — the resume is split into ~10 chunks.
    """

    id: Optional[int] = Field(default=None, primary_key=True)

    # CASCADE: when an application is deleted, its embeddings are deleted too
    application_id: int = Field(
        sa_column=Column(Integer, ForeignKey("application.id", ondelete="CASCADE"), nullable=False, index=True)
    )

    chunk_index: int  # 0, 1, 2... position within the resume

    # Original text — needed to return as RAG context to the LLM
    chunk_text: str = Field(sa_column=Column(Text, nullable=False))

    # The embedding vector. SQLModel cannot express pgvector types natively,
    # so we drop down to raw SQLAlchemy with the Vector column type.
    embedding: list[float] = Field(
        sa_column=Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=False)
    )

    created_at: datetime = Field(default_factory=datetime.now)


# --- SEMANTIC SEARCH SCHEMAS (Phase 2) ---
#
# Request/response models for POST /search/candidates. These are pure Pydantic
# schemas (no DB tables) for shaping the API surface.


class SearchQuery(SQLModel):
    """Request body for POST /search/candidates."""
    query: str = Field(min_length=3, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class SearchResult(SQLModel):
    """A single search hit — one candidate, with their best-matching resume chunk."""
    application_id: int
    candidate_name: str
    candidate_email: str
    resume_url: str
    job_id: int               # id of the parent job they originally applied to
    job_title: str            # parent job they originally applied to
    best_match_chunk: str     # the resume passage that matched the query
    similarity: float         # 0.0–1.0 — LLM score if rerank ran (0.01–1.0), else cosine
    critique: str | None = None  # LLM-generated reasoning when rerank succeeded


class SearchResponse(SQLModel):
    """Wraps the result list with pagination hints for the frontend."""
    results: list[SearchResult]
    total_returned: int       # length of results — convenience for the frontend
    has_more: bool            # true if a full page was returned (more likely available)
    # True if LLM rerank failed and we returned vector-only ranking. The
    # dashboard can surface a small "AI rerank unavailable" notice.
    degraded: bool = False


class JobEmbedding(SQLModel, table=True):
    """
    A single embedded chunk of a job description.
    Multiple rows per job — long job descriptions are chunked just like resumes.
    """

    id: Optional[int] = Field(default=None, primary_key=True)

    # CASCADE: when a job is deleted, its embeddings are deleted too
    job_id: int = Field(
        sa_column=Column(Integer, ForeignKey("joblisting.id", ondelete="CASCADE"), nullable=False, index=True)
    )

    chunk_index: int

    chunk_text: str = Field(sa_column=Column(Text, nullable=False))

    embedding: list[float] = Field(
        sa_column=Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=False)
    )

    created_at: datetime = Field(default_factory=datetime.now)


# --- CROSS-JOB MATCHING (Phase 3) ---
#
# Stores computed alternative-job suggestions for each application.
# Populated by `match_jobs_task` after resume embedding completes, and on
# manual recheck via the dashboard.


class CrossJobMatch(SQLModel, table=True):
    """
    A single computed match between an application and an alternative job
    posting within the same recruiter's pool.

    The `similarity` value is the aggregate top-3-chunk average score described
    in docs/ai-features/phase-3-cross-job-matching.md.
    """

    __table_args__ = (
        UniqueConstraint("application_id", "matched_job_id", name="uq_cross_job_match"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    application_id: int = Field(
        sa_column=Column(Integer, ForeignKey("application.id", ondelete="CASCADE"), nullable=False, index=True)
    )

    matched_job_id: int = Field(
        sa_column=Column(Integer, ForeignKey("joblisting.id", ondelete="CASCADE"), nullable=False, index=True)
    )

    similarity: float
    # LLM-generated reasoning explaining why this job is a match — populated
    # by the Phase 5 rerank step. Null if rerank failed and we fell back to
    # vector-only scoring.
    critique: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class CrossJobMatchResult(SQLModel):
    """Response shape for GET /applications/{id}/matches — one suggested alternative job."""
    matched_job_id: int
    job_title: str
    similarity: float
    critique: str | None = None


class CrossApplicantResult(SQLModel):
    """
    Response shape for GET /jobs/{job_id}/cross-applicants — the inverse view of
    CrossJobMatch. One row = a candidate who applied to a *different* job owned
    by the same recruiter but is a strong fit for *this* job.
    """
    application_id: int
    candidate_name: str
    candidate_email: str
    resume_url: str
    original_job_id: int
    original_job_title: str
    similarity: float
    critique: str | None = None


# --- RAG Q&A SCHEMAS (Phase 4) ---
#
# Request body for POST /applications/{id}/chat. The response is a server-sent
# event stream (not a Pydantic model) — see docs/ai-features/phase-4-rag-qa.md
# for the SSE event schema.


class ChatTurn(BaseModel):
    """One turn in the multi-turn conversation history sent by the frontend."""
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """
    Request body for POST /applications/{id}/chat.

    Conversation history is stored server-side in Redis, keyed by
    `(user_id, application_id, session_id)`. The frontend only needs to send
    a session_id; the backend loads the prior turns from Redis, runs the LLM,
    and persists the new turn. A fresh `session_id` from the client starts
    a new conversation. See app/chat_history.py.
    """
    question: str = Field(min_length=3, max_length=1000)
    session_id: str = Field(min_length=8, max_length=64)


class Citation(BaseModel):
    """A resume chunk that supported the LLM's answer; returned as the citations SSE event."""
    chunk_index: int
    chunk_text: str
    similarity: float
