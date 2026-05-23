# ---------------------------------------------------------------------------
# Purpose: Pydantic Models and SQLModel Tables
# ---------------------------------------------------------------------------

from datetime import datetime
from typing import Optional

from pydantic import (
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
    # When either is non-null, status becomes "failed" and the dashboard offers a Retry button.
    scoring_error: Optional[str] = None
    embedding_error: Optional[str] = None

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
