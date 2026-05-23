"""
Phase 1 smoke test.

Validates the resume chunking + embedding pipeline end-to-end by running
the embed_resume_task synchronously (in-process) and asserting that:

  1. A real multi-paragraph resume text gets chunked into multiple pieces
  2. Each chunk has a 768-dim embedding stored in resume_embedding
  3. Chunk indices are 0..N-1 with no gaps
  4. Cascade delete via JobListing removes all chunks (verifies FK CASCADE)

Run from the project root:

    docker compose exec worker python scripts/smoke_test_phase1.py
"""

import sys
from pathlib import Path

# Ensure the project root is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.models import Application, JobListing, ResumeEmbedding
from app.worker import embed_resume_task


# Multi-paragraph sample resume — long enough to chunk into several pieces
SAMPLE_RESUME = """
Jane Smith
Senior Software Engineer
jane.smith@example.com  |  +1-555-0123  |  San Francisco, CA  |  linkedin.com/in/janesmith

EXPERIENCE

Acme Corp, San Francisco, CA — Senior Backend Engineer
2022 — Present
Led the migration of 12 microservices from monolithic architecture to AWS ECS, reducing
deployment time from 45 minutes to under 5 minutes. Designed and built a real-time event
streaming pipeline using Kafka and Python that processes 2 million events per day. Mentored
a team of 4 junior engineers, conducting weekly code reviews and pair programming sessions.

Beta Industries, Remote — Software Engineer
2019 — 2022
Built and maintained a Django-based REST API serving 50,000 daily active users. Implemented
a caching layer using Redis that reduced average response time from 800ms to 120ms. Owned the
CI/CD pipeline using GitHub Actions, including automated testing, security scanning, and
canary deployments.

EDUCATION

University of California, Berkeley — BS Computer Science
2015 — 2019
GPA: 3.8 / 4.0

SKILLS

Languages: Python, Go, TypeScript, SQL
Frameworks: Django, FastAPI, React, Next.js
Infrastructure: AWS (ECS, S3, RDS, Lambda), Docker, Kubernetes, Terraform
Databases: PostgreSQL, Redis, DynamoDB, Elasticsearch
"""


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def main() -> None:
    print("=== Phase 1 smoke test — resume embedding pipeline ===\n")

    create_db_and_tables()
    _ok("Database is initialised")

    # Create the parent rows (job and application) so the FK constraint is satisfied
    with Session(engine) as session:
        test_job = JobListing(
            title="Phase 1 Smoke Test Role",
            description="Temporary job created by the embeddings smoke test.",
            skills="python, aws",
            location="remote",
        )
        session.add(test_job)
        session.commit()
        session.refresh(test_job)

        test_app = Application(
            job_id=test_job.id,
            candidate_email="phase1@example.com",
            candidate_name="Phase 1 Smoke Test",
            resume_url="/download/phase1.pdf",
        )
        session.add(test_app)
        session.commit()
        session.refresh(test_app)
        test_app_id = test_app.id
        test_job_id = test_job.id

    _ok(f"Created test job ({test_job_id}) and application ({test_app_id})")

    # Run the task synchronously so we can assert directly on its result.
    # .apply() runs in-process; .delay() would dispatch to the Redis queue.
    result = embed_resume_task.apply(
        kwargs={"text_content": SAMPLE_RESUME, "application_id": test_app_id}
    ).get()

    if result.get("status") != "success":
        _fail(f"Task did not return success: {result!r}")
    chunk_count = result.get("chunks", 0)
    if chunk_count < 2:
        _fail(f"Expected the resume to split into multiple chunks, got {chunk_count}")
    _ok(f"embed_resume_task returned success with {chunk_count} chunks")

    # Verify what landed in the DB
    with Session(engine) as session:
        rows = session.exec(
            select(ResumeEmbedding)
            .where(ResumeEmbedding.application_id == test_app_id)
            .order_by(ResumeEmbedding.chunk_index)
        ).all()

        if len(rows) != chunk_count:
            _fail(f"DB has {len(rows)} chunks but task reported {chunk_count}")
        _ok(f"DB contains {len(rows)} chunks for the test application")

        # Chunk indices should be contiguous 0..N-1
        expected_indices = list(range(chunk_count))
        actual_indices = [r.chunk_index for r in rows]
        if actual_indices != expected_indices:
            _fail(f"Chunk indices not contiguous: {actual_indices}")
        _ok("Chunk indices are contiguous 0..N-1")

        # Each row should have a 768-dim embedding and non-empty chunk_text
        for r in rows:
            if not r.chunk_text or len(r.chunk_text.strip()) == 0:
                _fail(f"Chunk {r.chunk_index} has empty text")
            if len(r.embedding) != 768:
                _fail(f"Chunk {r.chunk_index} embedding has {len(r.embedding)} dims, expected 768")
        _ok("All chunks have 768-dim embeddings and non-empty text")

        # Cleanup — delete the job, cascades to application and embeddings
        test_job = session.get(JobListing, test_job_id)
        session.delete(test_job)
        session.commit()

        remaining = session.exec(
            select(ResumeEmbedding).where(ResumeEmbedding.application_id == test_app_id)
        ).all()
        if remaining:
            _fail(f"Cascade delete failed — {len(remaining)} chunks still present")
        _ok("Cascade delete removed all chunks via job → application → embedding FKs")

    print("\n=== Phase 1 smoke test passed ===")


if __name__ == "__main__":
    main()
